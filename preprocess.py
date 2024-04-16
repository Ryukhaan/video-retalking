import sys

if sys.version_info[0] < 3 and sys.version_info[1] < 2:
    raise Exception("Must be using >= Python 3.2")

from os import listdir, path

if not path.isfile('checkpoints/s3fd.pth'):
    raise FileNotFoundError('Save the s3fd model to checkpoints/s3fd.pth \
							before running this script!')

import multiprocessing as mp
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
import argparse, os, cv2, traceback, subprocess
from tqdm import tqdm
from glob import glob
import futils.audio as audio
from futils.hparams import hparams as hp

import third_part.face_detection as face_detection
from third_part.face3d.util.load_mats import load_lm3d
from futils.inference_utils import split_coeff, load_face3d_net, load_DNet

import gc
import torch
from torchaudio import torch_load
from torch.nn.functional import pad
from encodec import EncodecModel
from encodec.utils import convert_audio
import clip

parser = argparse.ArgumentParser()

parser.add_argument('--ngpu', help='Number of GPUs across which to run in parallel', default=1, type=int)
parser.add_argument('--batch_size', help='Single GPU Face detection batch size', default=32, type=int)
parser.add_argument("--data_root", help="Root folder of the LRS2 dataset", required=True)
parser.add_argument("--preprocessed_root", help="Root folder of the preprocessed dataset", required=True)

parser.add_argument("--bandwidth", default=24.0, type=float, help="Bandwidth value (by default 24.0)")
parser.add_argument("--t", default=5, type=int, help="Number of frames as input")
parser.add_argument("--fps", help="Frame per second (default 25)", default=25, type=int)

args = parser.parse_args()

# Device Cuda or CPU and then set cache empty
device = 'cuda' if torch.cuda.is_available() else 'cpu'
gc.collect()
torch.cuda.empty_cache()

# Load encodec
audios_model = [EncodecModel.encodec_model_24khz() for id in range(args.ngpu)]
for m in audios_model:
    m.set_target_bandwidth(args.bandwidth)

fa = [face_detection.FaceAlignment(face_detection.LandmarksType._2D, flip_input=False,
                                   device='cuda:{}'.format(id)) for id in range(args.ngpu)]

template = 'ffmpeg -loglevel panic -y -i {} -strict -2 {}'


# template2 = 'ffmpeg -hide_banner -loglevel panic -threads 1 -y -i {} -async 1 -ac 1 -vn -acodec pcm_s16le -ar 16000 {}'

def process_video_file(vfile, args, gpu_id):
    video_stream = cv2.VideoCapture(vfile)

    frames = []
    while 1:
        still_reading, frame = video_stream.read()
        if not still_reading:
            video_stream.release()
            break
        frames.append(frame)

    vidname = os.path.basename(vfile).split('.')[0]
    dirname = vfile.split('/')[-2]

    fulldir = path.join(args.preprocessed_root, dirname, vidname)
    os.makedirs(fulldir, exist_ok=True)

    batches = [frames[i:i + args.batch_size] for i in range(0, len(frames), args.batch_size)]

    i = -1
    for fb in batches:
        preds = fa[gpu_id].get_detections_for_batch(np.asarray(fb))

        for j, f in enumerate(preds):
            i += 1
            if f is None:
                continue

            x1, y1, x2, y2 = f
            cv2.imwrite(path.join(fulldir, '{}.jpg'.format(i)), fb[j][y1:y2, x1:x2])


def process_audio_file(vfile, args):
    vidname = os.path.basename(vfile).split('.')[0]
    dirname = vfile.split('/')[-2]

    fulldir = path.join(args.preprocessed_root, dirname, vidname)
    os.makedirs(fulldir, exist_ok=True)

    wavpath = path.join(fulldir, 'audio.wav')

    command = template.format(vfile, wavpath)
    subprocess.call(command, shell=True)

def encode_audio(vfile, model, gpu_id):
    # Load audio
    wav, sr =  torch_load(vfile)

    # Pad wav to get NoF codec
    samples_per_frame = int(0.2 * sr)
    idx_multiplier, codes_chunks = int(1. / args.fps * sr), []

    vidname = os.path.basename(vfile).split('.')[0]
    dirname = vfile.split('/')[-2]

    fulldir = path.join(args.preprocessed_root, dirname, vidname)
    os.makedirs(fulldir, exist_ok=True)

    audio_chunks, i = [], 0
    while 1:
        start_idx = int(i * idx_multiplier)
        if start_idx + samples_per_frame > len(wav[0]):
            break
        chunk = wav[:, start_idx: start_idx + samples_per_frame]
        audio_chunks.append(chunk)

    batches = [audio_chunks[i:i + args.batch_size] for i in range(0, len(audio_chunks), args.batch_size)]

    for batch in batches:
        chunk = convert_audio(batch, sr, model.sample_rate, model.channels)

        # Extract discrete codes from EnCodec
        with torch.no_grad():
            encoded_frames = audios_model[gpu_id].encode(chunk)
        codes = torch.cat([encoded[0] for encoded in encoded_frames], dim=-1)  # [B, n_q, T]
        codes_chunks.append(np.array(codes))

    np.save(path.join(fulldir, 'audio_features.npy'), np.array(codes_chunks))

def mp_handler(job):
    vfile, args, gpu_id = job
    try:
        process_video_file(vfile, args, gpu_id)
    except KeyboardInterrupt:
        exit(0)
    except:
        traceback.print_exc()

def mp_encodec_handler(job):
    vfile, args, gpu_id = job
    try:
        encode_audio(vfile, args, gpu_id)
    except KeyboardInterrupt:
        exit(0)
    except:
        traceback.print_exc()

def main(args):
    print('Started processing for {} with {} GPUs'.format(args.data_root, args.ngpu))

    filelist = glob(path.join(args.data_root, '*/*.mp4'))

    # Filter list
    filelist = [vfile for vfile in filelist \
                    if not os.path.isdir(path.join(args.preprocessed_root,
                                               vfile.split('/')[-2],
                                               os.path.basename(vfile).split('.')[0]))]

    jobs = [(vfile, args, i % args.ngpu) for i, vfile in enumerate(filelist)]
    p = ThreadPoolExecutor(args.ngpu)
    futures = [p.submit(mp_handler, j) for j in jobs]
    _ = [r.result() for r in tqdm(as_completed(futures), total=len(futures))]

    print('Dumping audios...')
    for vfile in tqdm(filelist):
        try:
            process_audio_file(vfile, args)
        except KeyboardInterrupt:
            exit(0)
        except:
            traceback.print_exc()
            continue

    print("Extract Encodec Features")
    filelist = glob((path.join(args.data_root, '*/*/*.wav')))
    jobs = [(vfile, args, i % args.ngpu) for i, vfile in enumerate(filelist)]
    p = ThreadPoolExecutor(args.ngpu)
    futures = [p.submit(mp_encodec_handler, j) for j in jobs]
    _ = [r.result() for r in tqdm(as_completed(futures), total=len(futures))]


if __name__ == '__main__':
    main(args)