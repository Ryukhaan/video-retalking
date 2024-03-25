import glob
from os.path import dirname, join, basename, isfile

# Training Tensorboard
import matplotlib.pyplot as plt
from torch.utils.tensorboard import SummaryWriter

import json
import gc
import torch
from torch import nn
from torch import optim
import torchvision
from torchsummary import summary
from torch.utils import data as data_utils
from librosa import get_duration
import clip
import numpy as np
import cv2, os, sys, subprocess, platform, torch
from tqdm import tqdm
from PIL import Image
from scipy.io import loadmat
from scipy.io import wavfile

from pydub import AudioSegment
from pydub.utils import make_chunks

import matplotlib.pyplot as plt
from scipy.signal import correlate

# Audio framework and Encodec
#from encodec import EncodecModel
#from encodec.utils import convert_audio
import torchaudio

from sklearn.model_selection import train_test_split

from models.LNet import LNet
import pickle
from models import losses
import preprocessing.facing as preprocessing

sys.path.append('third_part')
# 3dmm extraction
from third_part.face3d.util.preprocess import align_img
from third_part.face3d.util.load_mats import load_lm3d
from third_part.face3d.extract_kp_videos import KeypointExtractor
# face enhancement
#from third_part.GPEN.face_enhancement import FaceEnhancement
#from third_part.GFPGAN.gfpgan import GFPGANer
# expression control
from third_part.ganimation_replicate.model.ganimation import GANimationModel

from futils import audio
from futils.ffhq_preprocess import Croper
from futils.alignment_stit import crop_faces, calc_alignment_coefficients, paste_image
from futils.inference_utils import Laplacian_Pyramid_Blending_with_mask, face_detect, load_train_model, train_options, split_coeff, \
                                  trans_image, transform_semantic, find_crop_norm_ratio, load_face3d_net, exp_aus_dict, save_checkpoint
from futils.inference_utils import load_model as fu_load_model
from futils import hparams, audio
import warnings
warnings.filterwarnings("ignore")

args = train_options()
hparams = hparams.hparams
lnet_T = 5
global_step = 0
global_epoch = 0
def get_image_list(data_root, split):
    filelist = []
    with open('./filelists/{}.txt'.format(split)) as f:
        for line in f:
            line = line.rstrip()
            if line.split('.')[-1] == 'mp4':
                filelist.append(os.path.join(data_root, line))
    return filelist

class Dataset(object):

    def __init__(self, filenames, device):
        global args
        self.args = args
        self.device = device
        self.all_videos = filenames #get_image_list(args.data_root, split)
        self.preprocessor = preprocessing.Preprocessor(args=None)
        self.net_recon = None

        # Dictionary for phoneme
        # Decrepated
        with open('./dictionary/english_mfa_v2_0_0.json', 'r') as file:
            self.dictionary = json.load(file)
        self.dictionary = self.dictionary['phones']
        self.dictionary.insert(0, 'spn')

        # Face detection
        self.kp_extractor = None
        self.idx = 0
        self.croper = None

        # Import CLIP for text encoding
        self.clip_model, self.preprocess_clip = clip.load("ViT-B/32", device=device)
        for param in self.clip_model.parameters():
            param.required_grad = False


    # Weird function
    def get_frame_id(self, frame):
        return int(basename(frame).split('.')[0])

    def read_video(self, index):
        video_stream = cv2.VideoCapture(self.all_videos[index])
        self.fps = video_stream.get(cv2.CAP_PROP_FPS)
        self.full_frames = np.load(self.all_videos[self.idx].split('.')[0] + "_cropped.npy")
        #self.full_frames = []
        #while True:
        #    still_reading, frame = video_stream.read()
        #    if not still_reading:
        #        video_stream.release()
        #        break
        #    y1, y2, x1, x2 = self.args.crop
        #    if x2 == -1: x2 = frame.shape[1]
        #    if y2 == -1: y2 = frame.shape[0]
        #    frame = frame[y1:y2, x1:x2]
        #    self.full_frames.append(frame)
        return self.full_frames

    def get_segmented_window(self, start_frame):
        assert lnet_T == 5
        if start_frame >= 0:
            return None
        return self.full_frames[start_frame:start_frame+lnet_T]

    def get_segmented_codes(self, index, start_frame):
        assert lnet_T == 5
        if start_frame >= 0:
            return None
        codes = np.load(self.all_videos[index].split('.')[0] + "_codes.npy",
                allow_pickle=True)
        codes = codes[start_frame: start_frame+lnet_T]
        codes = codes.reshape(-1, 32 * 15)
        return codes

    def get_segmented_mels(self, spec, start_frame):
        mels = []
        syncnet_mel_step_size = 16
        assert lnet_T == 5
        if start_frame >= 0: return None
        for i in range(start_frame, start_frame + lnet_T):
            m = self.crop_audio_window(spec, i - 2)
            if m.shape[0] != syncnet_mel_step_size:
                return None
            mels.append(m.T)
        mels = np.asarray(mels)
        return mels

    def get_segmented_phones(self, index, start_frame):
        assert lnet_T == 5
        if start_frame < 1: return None
        # Get folder and file without ext.
        basefile = self.all_videos[index].split('.')[0]
        with open(basefile + ".json", 'r', encoding='utf-8') as file:
            json_data = json.load(file)
        # Get Phones and words from json
        self.words = json_data['tiers']['words']['entries']
        #self.phones = json_data['tiers']['phones']

        m_fps = 1. / self.args.fps
        text_array = []
        for i in range(lnet_T):
            tmin = (start_frame + i) * m_fps
            tmax = (start_frame + i + 1) * m_fps
            tmp_word = []
            for (ts, te, word) in self.words:
                if ts < tmax and te >= tmin:
                    tmp_word.append(word)
            text_array.append(" ".join(tmp_word))
        with torch.no_grad():
            text_tokens = clip.tokenize(text_array).to(self.device)
            text_features = self.clip_model.encode_text(text_tokens)
        return text_features

    def crop_audio_window(self, spec, start_frame):
        syncnet_mel_step_size = 16
        start_idx = int(80. * (start_frame / float(hparams.fps)))
        end_idx = start_idx + syncnet_mel_step_size
        return spec[start_idx: end_idx, :]

    def prepare_window(self, window):
        # Convert to 3 x T x H x W
        x = np.asarray(window) / 255.
        x = np.transpose(x, (3, 0, 1, 2))
        return x

    def landmarks_estimate(self, nframes, save=False, start_frame=0):
        # face detection & cropping, cropping the first frame as the style of FFHQ
        croper = Croper('checkpoints/shape_predictor_68_face_landmarks.dat')
        full_frames_RGB = [cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) for frame in nframes]
        try:
            full_frames_RGB, crop, quad = croper.crop(full_frames_RGB, xsize=512) # Why 512 ?
        except TypeError:
            return 1
        return 0

        clx, cly, crx, cry = crop
        lx, ly, rx, ry = quad
        lx, ly, rx, ry = int(lx), int(ly), int(rx), int(ry)
        oy1, oy2, ox1, ox2 = cly +ly, min(cly +ry, nframes[0].shape[0]), clx +lx, min(clx +rx, nframes[0].shape[1])
        self.coordinates = oy1, oy2, ox1, ox2
        # original_size = (ox2 - ox1, oy2 - oy1)
        self.frames_pil = [Image.fromarray(cv2.resize(frame ,(256 ,256))) for frame in full_frames_RGB]

        # get the landmark according to the detected face.
        # Change this one
        if not os.path.isfile(self.all_videos[self.idx].split('.')[0] +'_landmarks.txt') or save:
            torch.cuda.empty_cache()
            #print('[Step 1] Landmarks Extraction in Video.')
            if self.kp_extractor is None:
                self.kp_extractor = KeypointExtractor()
            if not save:
                self.lm = self.kp_extractor.extract_keypoint(self.frames_pil)
            else:
                print("Save")
                self.lm = self.kp_extractor.extract_keypoint(self.frames_pil, self.all_videos[self.idx].split('.')[0] + '_landmarks.txt')
        else:
            #print('[Step 1] Using saved landmarks.')
            self.lm = np.loadtxt( self.all_videos[self.idx].split('.')[0] +'_landmarks.txt').astype(np.float32)
            self.lm = self.lm[start_frame:start_frame+2*lnet_T]
            self.lm = self.lm.reshape([lnet_T, -1, 2])

    def face_3dmm_extraction(self, save=False, start_frame=0):
        torch.cuda.empty_cache()
        if not os.path.isfile(self.all_videos[self.idx].split('.')[0] + "_coeffs.npy"):
            if self.net_recon is None:
                self.net_recon = load_face3d_net(self.args.face3d_net_path, device)
            lm3d_std = load_lm3d('checkpoints/BFM')
            video_coeffs = []
            for idx in range(len(self.frames_pil)):#, desc="[Step 2] 3DMM Extraction In Video:"):
                frame = self.frames_pil[idx]
                W, H = frame.size
                lm_idx = self.lm[idx].reshape([-1, 2])
                if np.mean(lm_idx) == -1:
                    lm_idx = (lm3d_std[:, :2 ] +1) / 2.
                    lm_idx = np.concatenate([lm_idx[:, :1] * W, lm_idx[:, 1:2] * H], 1)
                else:
                    lm_idx[:, -1] = H - 1 - lm_idx[:, -1]

                trans_params, im_idx, lm_idx, _ = align_img(frame, lm_idx, lm3d_std)
                trans_params = np.array([float(item) for item in np.hsplit(trans_params, 5)]).astype(np.float32)
                im_idx_tensor = torch.tensor(np.array(im_idx ) /255., dtype=torch.float32).permute(2, 0, 1).to \
                    (device).unsqueeze(0)
                with torch.no_grad():
                    coeffs = split_coeff(self.net_recon(im_idx_tensor))

                pred_coeff = {key :coeffs[key].cpu().numpy() for key in coeffs}
                pred_coeff = np.concatenate([pred_coeff['id'], pred_coeff['exp'], pred_coeff['tex'], pred_coeff['angle'], \
                                             pred_coeff['gamma'], pred_coeff['trans'], trans_params[None]], 1)
                video_coeffs.append(pred_coeff)
            self.semantic_npy = np.array(video_coeffs)[: ,0]
            if save:
                np.save( self.all_videos[self.idx].split('.')[0] +'_coeffs.npy', self.semantic_npy)
        else:
            self.semantic_npy = np.load(self.all_videos[self.idx].split('.')[0] + "_coeffs.npy").astype(np.float32)
            self.semantic_npy = self.semantic_npy[start_frame:start_frame+lnet_T]
    def hack_3dmm_expression(self, save=False, start_frame=0):
        expression = torch.tensor(loadmat('checkpoints/expression.mat')['expression_center'])[0]

        # Video Image Stabilized
        if not os.path.isfile( self.all_videos[self.idx].split('.')[0] + '_stablized.npy'):
            self.imgs = []
            for idx in range(len(self.frames_pil)): #desc="[Step 3] Stablize the expression In Video:"):
                if self.args.one_shot:
                    source_img = trans_image(self.frames_pil[0]).unsqueeze(0).to(device)
                    semantic_source_numpy = self.semantic_npy[0:1]
                else:
                    source_img = trans_image(self.frames_pil[idx]).unsqueeze(0).to(device)
                    semantic_source_numpy = self.semantic_npy[idx:idx +1]
                ratio = find_crop_norm_ratio(semantic_source_numpy, self.semantic_npy)
                coeff = transform_semantic(self.semantic_npy, idx, ratio).unsqueeze(0).to(device)

                # hacking the new expression
                coeff[:, :64, :] = expression[None, :64, None].to(device)
                with torch.no_grad():
                    output = self.D_Net(source_img, coeff)
                img_stablized = np.uint8 \
                    ((output['fake_image'].squeeze(0).permute(1 ,2 ,0).cpu().clamp_(-1, 1).numpy() + 1 )/ 2. * 255)
                self.imgs.append(cv2.cvtColor(img_stablized, cv2.COLOR_RGB2BGR))
            if save:
                np.save(self.all_videos[self.idx].split('.')[0] + '_stablized.npy', self.imgs)
            #del D_Net, model
            torch.cuda.empty_cache()
        else:
            self.imgs = np.load( self.all_videos[self.idx].split('.')[0] + "_stablized.npy")
            self.imgs = self.imgs[start_frame:start_frame+lnet_T]
    def __len__(self):
        return len(self.all_videos)

    def __getitem__(self, idx):
        img_size = 384
        size = torch.Size([30, 96, 96])
        while True:
            # Take a random video
            self.idx = np.random.randint(0, len(self.all_videos) - 1)

            # Read full frames from that video
            vidname = self.all_videos[self.idx]
            frames = self.read_video(self.idx)

            # Take a random frame
            start_frame = np.random.randint(0, len(frames) - lnet_T)

            # Get sub full frames
            nframes = self.get_segmented_window(start_frame)

            # Get Encodec features from audio
            codes  = self.get_segmented_codes(self.idx, start_frame)

            # Get CLIP features from text
            phones = self.get_segmented_phones(self.idx, start_frame)

            # Read WAV for Wav2Lip Sync Loss
            try:
                wavpath = vidname.split('.')[0] + '.wav'
                wav = audio.load_wav(wavpath, hparams.sample_rate)
                orig_mel = audio.melspectrogram(wav).T
                indiv_mels = self.get_segmented_mels(orig_mel.copy(), start_frame)
            except Exception as e:
                print("Error in wav or mel")
                continue
            mel = self.crop_audio_window(orig_mel.copy(), start_frame)

            # Get landmarks
            if not self.landmarks_estimate(nframes, save=False, start_frame=start_frame):
                continue

            nframes = self.full_frames_RGB
            # Get Hack Image
            try:
                self.hack_3dmm_expression(save=False, start_frame=start_frame)
            except Exception as e:
                continue

            if len(self.imgs.shape) <= 3:
                continue
            window = self.prepare_window(nframes)
            if window.shape[1] != 5:
                continue
            self.imgs = np.asarray([cv2.resize(frame, (96,96)) for frame in self.imgs])
            try:
                stabilized_window = self.prepare_window(self.imgs)
            except Exception as e:
                continue
            self.imgs_masked = self.imgs.copy()

            masked_window = self.prepare_window(self.imgs_masked)
            masked_window[:, window.shape[2] // 2:] = 0.
            x = np.concatenate([masked_window, stabilized_window], axis=0)
            if x.shape != torch.Size([6, 5, 96, 96]):
                continue

            y = window.copy()
            y = torch.FloatTensor(y)

            codes = torch.FloatTensor(codes)
            #phones = phones
            x = torch.FloatTensor(x)
            mel = torch.FloatTensor(mel.T).unsqueeze(0)
            #indiv_mels = torch.FloatTensor(indiv_mels).unsqueeze(1)

            return x, codes, phones, mel, y

    def get_crop_orig_images(self):
        # face detection & cropping, cropping the first frame as the style of FFHQ
        if self.croper is None:
            self.croper = Croper('checkpoints/shape_predictor_68_face_landmarks.dat')
        full_frames_RGB = [cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) for frame in self.full_frames]
        full_frames_RGB, crop, quad = self.croper.crop(full_frames_RGB, xsize=512) # Why 512 ?

        clx, cly, crx, cry = crop
        lx, ly, rx, ry = quad
        lx, ly, rx, ry = int(lx), int(ly), int(rx), int(ry)
        oy1, oy2, ox1, ox2 = cly +ly, min(cly +ry, self.full_frames[0].shape[0]), clx +lx, min(clx +rx, self.full_frames[0].shape[1])
        coordinates = oy1, oy2, ox1, ox2
        # original_size = (ox2 - ox1, oy2 - oy1)
        frames_pil = np.array([Image.fromarray(cv2.resize(frame ,(256 ,256))) for frame in full_frames_RGB])
        print("Save {}".format(self.all_videos[self.idx].split('.')[0] + "_cropped.npy"))
        np.save(self.all_videos[self.idx].split('.')[0] + "_cropped.npy", frames_pil)


    def save_preprocess(self):
        self.croper = None
        for idx, file in tqdm(enumerate(self.all_videos), total=len(self.all_videos)):
            self.idx = idx
            self.read_video(idx)
            self.get_crop_orig_images()
            #self.landmarks_estimate(self.full_frames, save=True)
            #self.face_3dmm_extraction(save=True)
            #self.hack_3dmm_expression(save=True)

def datagen(frames, mels, full_frames, frames_pil, cox):
    img_batch, mel_batch, frame_batch, coords_batch, ref_batch, full_frame_batch = [], [], [], [], [], []
    #base_name = args.face.split('/')[-1]
    refs = []
    image_size = 256

    # original frames
    kp_extractor = KeypointExtractor()

    fr_pil = [Image.fromarray(frame) for frame in frames]
    #fr_pil = frames.copy()
    lms = kp_extractor.extract_keypoint(fr_pil, 'temp/' + 'x12_landmarks.txt')
    frames_pil = [ (lm, frame) for frame,lm in zip(fr_pil, lms)] # frames is the croped version of modified face
    crops, orig_images, quads  = crop_faces(image_size, frames_pil, scale=1.0, use_fa=True)
    inverse_transforms = [calc_alignment_coefficients(quad + 0.5, [[0, 0], [0, image_size], [image_size, image_size], [image_size, 0]]) for quad in quads]
    del kp_extractor.detector

    oy1,oy2,ox1,ox2 = cox
    face_det_results = face_detect(full_frames, args, jaw_correction=True)

    for inverse_transform, crop, full_frame, face_det in zip(inverse_transforms, crops, full_frames, face_det_results):
        imc_pil = paste_image(inverse_transform, crop, Image.fromarray(
            cv2.resize(full_frame[int(oy1):int(oy2), int(ox1):int(ox2)], (256, 256))))

        ff = full_frame.copy()
        ff[int(oy1):int(oy2), int(ox1):int(ox2)] = cv2.resize(np.array(imc_pil.convert('RGB')), (ox2 - ox1, oy2 - oy1))
        oface, coords = face_det
        y1, y2, x1, x2 = coords
        refs.append(ff[y1: y2, x1:x2])

    for i, m in enumerate(mels):
        idx = 0 if args.static else i % len(frames)
        frame_to_save = frames[idx].copy()
        face = refs[idx]
        oface, coords = face_det_results[idx].copy()

        face = cv2.resize(face, (args.img_size, args.img_size))
        oface = cv2.resize(oface, (args.img_size, args.img_size))

        #change face and oface in img and ref
        cv2.imwrite("./temp/sframe.png", frame_to_save)
        cv2.imwrite("./temp/face.png", face)
        cv2.imwrite("./temp/oface.png", oface)
        img_batch.append(oface)
        ref_batch.append(face)
        mel_batch.append(m)
        coords_batch.append(coords)
        frame_batch.append(frame_to_save)
        full_frame_batch.append(full_frames[idx].copy())

def get_crop_orig_images(full_frames, idx, all_videos, croper):
    # face detection & cropping, cropping the first frame as the style of FFHQ
    full_frames_RGB = [cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) for frame in full_frames]
    try:
        full_frames_RGB, crop, quad = croper.crop(full_frames_RGB, xsize=512) # Why 512 ?
    except Exception as e:
        return

    clx, cly, crx, cry = crop
    lx, ly, rx, ry = quad
    lx, ly, rx, ry = int(lx), int(ly), int(rx), int(ry)
    oy1, oy2, ox1, ox2 = cly +ly, min(cly +ry, full_frames[0].shape[0]), clx +lx, min(clx +rx, full_frames[0].shape[1])
    coordinates = oy1, oy2, ox1, ox2
    # original_size = (ox2 - ox1, oy2 - oy1)
    frames_pil = np.array([Image.fromarray(cv2.resize(frame ,(256 ,256))) for frame in full_frames_RGB])
    #print("Save {}".format(all_videos[idx].split('.')[0] + "_cropped.npy"))
    np.save(all_videos[idx].split('.')[0] + "_cropped.npy", frames_pil)

def read_video(idx, all_videos):
    video_stream = cv2.VideoCapture(all_videos[idx])
    fps = video_stream.get(cv2.CAP_PROP_FPS)
    #full_frames = np.load(all_videos[idx].split('.')[0] + "_cropped.npy")
    full_frames = []
    while True:
       still_reading, frame = video_stream.read()
       if not still_reading:
           video_stream.release()
           break
       y1, y2, x1, x2 = [0, -1, 0, -1] #self.args.crop
       if x2 == -1: x2 = frame.shape[1]
       if y2 == -1: y2 = frame.shape[0]
       frame = frame[y1:y2, x1:x2]
       full_frames.append(frame)
    return full_frames

def save_preprocess(all_videos):
    croper = Croper('checkpoints/shape_predictor_68_face_landmarks.dat')
    for idx, file in tqdm(enumerate(all_videos), total=len(all_videos)):
        full_frames = read_video(idx, all_videos)
        get_crop_orig_images(full_frames, idx, all_videos, croper)

def train(device, model, train_data_loader, test_data_loader, optimizer,
          checkpoint_dir=None, checkpoint_interval=None, nepochs=None):

    global global_step, global_epoch
    resumed_step = global_step
    loss_func = losses.LoraLoss(device)
    writer = SummaryWriter()
    while global_epoch < nepochs:
        running_loss = 0.
        prog_bar = tqdm(enumerate(train_data_loader), total=len(train_data_loader)+1)
        for step, (x, code, phone, mel, y) in prog_bar:
            if x is None: continue
            model.train()
            optimizer.zero_grad()

            x = x.to(device)
            code = code.to(device)
            phone = phone.to(device)
            pred = model(code, phone, x)
            if pred.shape != torch.Size([4, 3, 5, 96, 96]):
                continue

            pred = pred.to(device)
            y = y.to(device)
            mel = mel.to(device)
            loss = loss_func(pred, y, mel)

            global_step += 1
            cur_session_steps = global_step - resumed_step
            running_loss += loss.item()
            prog_bar.set_description('Loss: {}'.format(running_loss / (step + 1)))
            # Write Loss To TensorBoard
            writer.add_scalar('training loss', running_loss / (step + 1), global_epoch * len(train_data_loader) + step)
            if global_step % hparams.writer_interval == 0:
                writer.add_figure('predictions',
                                  plot_classes_preds(model, x, code, phone, y),
                                  global_step=global_epoch * len(train_data_loader) + step)
            prog_bar.refresh()
            if global_step == 1 or global_step % checkpoint_interval == 0:
                save_checkpoint(
                    model, optimizer, global_step, checkpoint_dir, global_epoch)

            if global_step % hparams.syncnet_eval_interval == 0:
                with torch.no_grad():
                    eval_model(test_data_loader, global_step, device, model, checkpoint_dir)

        global_epoch += 1
        prog_bar.close()
def datagen(frames, mels, full_frames, frames_pil, cox):
    img_batch, mel_batch, frame_batch, coords_batch, ref_batch, full_frame_batch = [], [], [], [], [], []
    base_name = args.face.split('/')[-1]
    refs = []
    image_size = 256

    # original frames
    kp_extractor = KeypointExtractor()
    fr_pil = [Image.fromarray(frame) for frame in frames]
    lms = kp_extractor.extract_keypoint(fr_pil, 'temp/'+base_name+'x12_landmarks.txt')
    frames_pil = [ (lm, frame) for frame,lm in zip(fr_pil, lms)] # frames is the croped version of modified face
    crops, orig_images, quads  = crop_faces(image_size, frames_pil, scale=1.0, use_fa=True)
    inverse_transforms = [calc_alignment_coefficients(quad + 0.5, [[0, 0], [0, image_size], [image_size, image_size], [image_size, 0]]) for quad in quads]
    del kp_extractor.detector

    oy1,oy2,ox1,ox2 = cox
    face_det_results = face_detect(full_frames, args, jaw_correction=True)

    for inverse_transform, crop, full_frame, face_det in zip(inverse_transforms, crops, full_frames, face_det_results):
        imc_pil = paste_image(inverse_transform, crop, Image.fromarray(
            cv2.resize(full_frame[int(oy1):int(oy2), int(ox1):int(ox2)], (256, 256))))

        ff = full_frame.copy()
        ff[int(oy1):int(oy2), int(ox1):int(ox2)] = cv2.resize(np.array(imc_pil.convert('RGB')), (ox2 - ox1, oy2 - oy1))
        oface, coords = face_det
        y1, y2, x1, x2 = coords
        refs.append(ff[y1: y2, x1:x2])

    for i, m in enumerate(mels):
        idx = 0 if args.static else i % len(frames)
        frame_to_save = frames[idx].copy()
        face = refs[idx]
        oface, coords = face_det_results[idx].copy()

        face = cv2.resize(face, (args.img_size, args.img_size))
        oface = cv2.resize(oface, (args.img_size, args.img_size))

        img_batch.append(oface)
        ref_batch.append(face)
        mel_batch.append(m)
        coords_batch.append(coords)
        frame_batch.append(frame_to_save)
        full_frame_batch.append(full_frames[idx].copy())

        if len(img_batch) >= args.LNet_batch_size:
            img_batch, mel_batch, ref_batch = np.asarray(img_batch), np.asarray(mel_batch), np.asarray(ref_batch)
            img_masked = img_batch.copy()
            img_original = img_batch.copy()
            img_masked[:, args.img_size//2:] = 0
            img_batch = np.concatenate((img_masked, ref_batch), axis=3) / 255.
            mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])

            yield img_batch, mel_batch, frame_batch, coords_batch, img_original, full_frame_batch
            img_batch, mel_batch, frame_batch, coords_batch, img_original, full_frame_batch, ref_batch  = [], [], [], [], [], [], []

    if len(img_batch) > 0:
        img_batch, mel_batch, ref_batch = np.asarray(img_batch), np.asarray(mel_batch), np.asarray(ref_batch)
        img_masked = img_batch.copy()
        img_original = img_batch.copy()
        img_masked[:, args.img_size//2:] = 0
        img_batch = np.concatenate((img_masked, ref_batch), axis=3) / 255.
        mel_batch = np.reshape(mel_batch, [len(mel_batch), mel_batch.shape[1], mel_batch.shape[2], 1])
        yield img_batch, mel_batch, frame_batch, coords_batch, img_original, full_frame_batch

def eval_model(test_data_loader, global_step, device, model, checkpoint_dir):
    eval_steps = 1400
    print('Evaluating for {} steps'.format(eval_steps))
    losses = []
    loss_func = losses.LoraLoss(device)
    while 1:
        prog_bar = tqdm(enumerate(test_data_loader), total=len(test_data_loader) + 1)
        for step, (x, code, phone, mel, y) in prog_bar:
            if x is None:
                continue
            model.eval()

            optimizer.zero_grad()

            x = x.to(device)
            mel = mel.to(device)
            y = y.to(device)
            code = code.to(device)
            phone = phone.to(device)
            pred = model(code, phone, x)
            if pred.shape != torch.Size([4, 3, 5, 96, 96]):
                continue
            mel = mel.to(device)
            loss = loss_func(pred, y, mel)

            losses.append(loss.item())
            prog_bar.set_description('Loss: {}'.format(sum(losses) / len(losses)))
            if step > eval_steps: break

        averaged_loss = sum(losses) / len(losses)
        print(averaged_loss)

        return

def save_checkpoint(model, optimizer, step, checkpoint_dir, epoch, prefix=''):
    checkpoint_path = join(
        checkpoint_dir, "{}checkpoint_step{:09d}.pth".format(prefix, global_step))
    optimizer_state = optimizer.state_dict() if hparams.save_optimizer_state else None
    torch.save({
        "state_dict": model.state_dict(),
        "optimizer": optimizer_state,
        "global_step": step,
        "global_epoch": epoch,
    }, checkpoint_path)
    print("Saved checkpoint:", checkpoint_path)

def _load(checkpoint_path):
    if use_cuda:
        checkpoint = torch.load(checkpoint_path)
    else:
        checkpoint = torch.load(checkpoint_path,
                                map_location=lambda storage, loc: storage)
    return checkpoint


def load_checkpoint(path, model, optimizer, reset_optimizer=False, overwrite_global_states=True):
    global global_step
    global global_epoch

    print("Load checkpoint from: {}".format(path))
    if not os.path.isfile(path):
        return model
    checkpoint = _load(path)
    s = checkpoint["state_dict"]
    new_s = {}
    for k, v in s.items():
        new_s[k.replace('module.', '')] = v
    model.load_state_dict(new_s)
    if not reset_optimizer:
        optimizer_state = checkpoint["optimizer"]
        if optimizer_state is not None:
            print("Load optimizer state from {}".format(path))
            optimizer.load_state_dict(checkpoint["optimizer"])
    if overwrite_global_states:
        global_step = checkpoint["global_step"]
        global_epoch = checkpoint["global_epoch"]

    return model

if __name__ == "__main__":
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")
    checkpoint_dir = args.checkpoint_dir
    checkpoint_path = args.checkpoint_path

    if not os.path.exists(checkpoint_dir):
        os.mkdir(checkpoint_dir)

    filenames = get_image_list(args.data_root, 'train')
    seed = 42
    train_list, val_list = train_test_split(np.array(filenames), random_state=seed, train_size=0.8, test_size=0.2)
    print(len(filenames), len(train_list), len(val_list))
    # Dataset and Dataloader setup
    save_preprocess(train_list)
    save_preprocess(val_list)
    exit(0)
    #train_dataset = Dataset(train_list, device)
    #train_dataset.save_preprocess()
    test_dataset = Dataset(val_list, device)
    #test_dataset.save_preprocess()

    train_data_loader = data_utils.DataLoader(
        train_dataset, batch_size=hparams.batch_size, shuffle=True)
        #num_workers=hparams.num_workers)
    test_data_loader = data_utils.DataLoader(
        test_dataset, batch_size=hparams.batch_size)
        #num_workers=8)


    # Model
    model = LNet().to(device)
    print('total trainable params {}'.format(sum(p.numel() for p in model.parameters() if p.requires_grad)))

    optimizer = optim.Adam([p for p in model.parameters() if p.requires_grad],
                           lr=hparams.syncnet_lr)

    print(checkpoint_dir, checkpoint_path)

    checkpoint_path = "checkpoints/Lnet.pth"
    #if checkpoint_path is not None:
    #    load_checkpoint(checkpoint_path, model, optimizer, reset_optimizer=False)
    #checkpoint_path = "checkpoints/Pnet.pth"
    #checkpoint_path = 'checkpoints/checkpoint_step000010000.pth'
    load_checkpoint(checkpoint_path, model, optimizer, reset_optimizer=False)

    train(device, model, train_data_loader, test_data_loader, optimizer,
          checkpoint_dir=checkpoint_dir,
          checkpoint_interval=hparams.syncnet_checkpoint_interval,
          nepochs=hparams.nepochs)
