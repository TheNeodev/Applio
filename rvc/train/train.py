import os
import re
import sys
import glob
import json
import torch
import datetime

import math
from collections import deque
from distutils.util import strtobool
from random import randint, shuffle
from time import time as ttime
from time import sleep
from tqdm import tqdm
import numpy as np
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.tensorboard import SummaryWriter
from torch.cuda.amp import GradScaler, autocast
from torch.utils.data import DataLoader
from torch.nn import functional as F

import torch.distributed as dist
import torch.multiprocessing as mp

now_dir = os.getcwd()
sys.path.append(os.path.join(now_dir))

# Zluda hijack
import rvc.lib.zluda

from utils import (
    HParams,
    plot_spectrogram_to_numpy,
    summarize,
    load_checkpoint,
    save_checkpoint,
    latest_checkpoint_path,
    load_wav_to_torch,
)

from losses import (
    discriminator_loss,
    feature_loss,
    generator_loss,
    kl_loss,
    envelope_loss,
)
from mel_processing import (
    mel_spectrogram_torch,
    spec_to_mel_torch,
    MultiScaleMelSpectrogramLoss,
)

from rvc.train.process.extract_model import extract_model

from rvc.lib.algorithm import commons

# Parse command line arguments
model_name = sys.argv[1]
save_every_epoch = int(sys.argv[2])
total_epoch = int(sys.argv[3])
pretrainG = sys.argv[4]
pretrainD = sys.argv[5]
version = sys.argv[6]
gpus = sys.argv[7]
batch_size = int(sys.argv[8])
sample_rate = int(sys.argv[9])
save_only_latest = strtobool(sys.argv[10])
save_every_weights = strtobool(sys.argv[11])
cache_data_in_gpu = strtobool(sys.argv[12])
overtraining_detector = strtobool(sys.argv[13])
overtraining_threshold = int(sys.argv[14])
cleanup = strtobool(sys.argv[15])
vocoder = sys.argv[16]
checkpointing = strtobool(sys.argv[17])

current_dir = os.getcwd()
experiment_dir = os.path.join(current_dir, "logs", model_name)
config_save_path = os.path.join(experiment_dir, "config.json")
dataset_path = os.path.join(experiment_dir, "sliced_audios")

with open(config_save_path, "r") as f:
    config = json.load(f)
config = HParams(**config)
config.data.training_files = os.path.join(experiment_dir, "filelist.txt")

torch.backends.cudnn.deterministic = False
torch.backends.cudnn.benchmark = True

global_step = 0
last_loss_gen_all = 0
overtrain_save_epoch = 0
loss_gen_history = []
smoothed_loss_gen_history = []
loss_disc_history = []
smoothed_loss_disc_history = []
lowest_value = {"step": 0, "value": float("inf"), "epoch": 0}
training_file_path = os.path.join(experiment_dir, "training_data.json")

avg_losses = {
    "gen_loss_queue": deque(maxlen=10),
    "disc_loss_queue": deque(maxlen=10),
    "disc_loss_50": deque(maxlen=50),
    "env_loss_50": deque(maxlen=50),
    "fm_loss_50": deque(maxlen=50),
    "kl_loss_50": deque(maxlen=50),
    "mel_loss_50": deque(maxlen=50),
    "gen_loss_50": deque(maxlen=50),
}

import logging

logging.getLogger("torch").setLevel(logging.ERROR)


class EpochRecorder:
    """
    Records the time elapsed per epoch.
    """

    def __init__(self):
        self.last_time = ttime()

    def record(self):
        """
        Records the elapsed time and returns a formatted string.
        """
        now_time = ttime()
        elapsed_time = now_time - self.last_time
        self.last_time = now_time
        elapsed_time = round(elapsed_time, 1)
        elapsed_time_str = str(datetime.timedelta(seconds=int(elapsed_time)))
        current_time = datetime.datetime.now().strftime("%H:%M:%S")
        return f"time={current_time} | training_speed={elapsed_time_str}"


def verify_checkpoint_shapes(checkpoint_path, model):
    checkpoint = torch.load(checkpoint_path, map_location="cpu")
    checkpoint_state_dict = checkpoint["model"]
    try:
        if hasattr(model, "module"):
            model_state_dict = model.module.load_state_dict(checkpoint_state_dict)
        else:
            model_state_dict = model.load_state_dict(checkpoint_state_dict)
    except RuntimeError:
        print(
            "The parameters of the pretrain model such as the sample rate or architecture do not match the selected model."
        )
        sys.exit(1)
    else:
        del checkpoint
        del checkpoint_state_dict
        del model_state_dict


def main():
    """
    Main function to start the training process.
    """
    global training_file_path, last_loss_gen_all, smoothed_loss_gen_history, loss_gen_history, loss_disc_history, smoothed_loss_disc_history, overtrain_save_epoch, gpus

    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = str(randint(20000, 55555))
    # Check sample rate
    wavs = glob.glob(
        os.path.join(os.path.join(experiment_dir, "sliced_audios"), "*.wav")
    )
    if wavs:
        _, sr = load_wav_to_torch(wavs[0])
        if sr != sample_rate:
            print(
                f"Error: Pretrained model sample rate ({sample_rate} Hz) does not match dataset audio sample rate ({sr} Hz)."
            )
            os._exit(1)
    else:
        print("No wav file found.")

    if torch.cuda.is_available():
        device = torch.device("cuda")
        gpus = [int(item) for item in gpus.split('-')]
        n_gpus = len(gpus) 
    elif torch.backends.mps.is_available():
        device = torch.device("mps")
        gpus = [0]
        n_gpus = 1
    else:
        device = torch.device("cpu")
        gpus = [0]
        n_gpus = 1
        print("Training with CPU, this will take a long time.")

    def start():
        """
        Starts the training process with multi-GPU support or CPU.
        """
        children = []
        pid_data = {"process_pids": []}
        with open(config_save_path, "r") as pid_file:
            try:
                existing_data = json.load(pid_file)
                pid_data.update(existing_data)
            except json.JSONDecodeError:
                pass
        with open(config_save_path, "w") as pid_file:
            for rank, device_id in enumerate(gpus):
                subproc = mp.Process(
                    target=run,
                    args=(
                        rank,
                        n_gpus,
                        experiment_dir,
                        pretrainG,
                        pretrainD,
                        total_epoch,
                        save_every_weights,
                        config,
                        device,
                        device_id,
                    ),
                )
                children.append(subproc)
                subproc.start()
                pid_data["process_pids"].append(subproc.pid)
            json.dump(pid_data, pid_file, indent=4)

        for i in range(n_gpus):
            children[i].join()

    def load_from_json(file_path):
        """
        Load data from a JSON file.

        Args:
            file_path (str): The path to the JSON file.
        """
        if os.path.exists(file_path):
            with open(file_path, "r") as f:
                data = json.load(f)
                return (
                    data.get("loss_disc_history", []),
                    data.get("smoothed_loss_disc_history", []),
                    data.get("loss_gen_history", []),
                    data.get("smoothed_loss_gen_history", []),
                )
        return [], [], [], []

    def continue_overtrain_detector(training_file_path):
        """
        Continues the overtrain detector by loading the training history from a JSON file.

        Args:
            training_file_path (str): The file path of the JSON file containing the training history.
        """
        if overtraining_detector:
            if os.path.exists(training_file_path):
                (
                    loss_disc_history,
                    smoothed_loss_disc_history,
                    loss_gen_history,
                    smoothed_loss_gen_history,
                ) = load_from_json(training_file_path)

    if cleanup:
        print("Removing files from the prior training attempt...")

        # Clean up unnecessary files
        for root, dirs, files in os.walk(
            os.path.join(now_dir, "logs", model_name), topdown=False
        ):
            for name in files:
                file_path = os.path.join(root, name)
                file_name, file_extension = os.path.splitext(name)
                if (
                    file_extension == ".0"
                    or (file_name.startswith("D_") and file_extension == ".pth")
                    or (file_name.startswith("G_") and file_extension == ".pth")
                    or (file_name.startswith("added") and file_extension == ".index")
                ):
                    os.remove(file_path)
            for name in dirs:
                if name == "eval":
                    folder_path = os.path.join(root, name)
                    for item in os.listdir(folder_path):
                        item_path = os.path.join(folder_path, item)
                        if os.path.isfile(item_path):
                            os.remove(item_path)
                    os.rmdir(folder_path)

        print("Cleanup done!")

    continue_overtrain_detector(training_file_path)
    start()


def run(
    rank,
    n_gpus,
    experiment_dir,
    pretrainG,
    pretrainD,
    custom_total_epoch,
    custom_save_every_weights,
    config,
    device,
    device_id,
):
    """
    Runs the training loop on a specific GPU or CPU.

    Args:
        rank (int): The rank of the current process within the distributed training setup.
        n_gpus (int): The total number of GPUs available for training.
        experiment_dir (str): The directory where experiment logs and checkpoints will be saved.
        pretrainG (str): Path to the pre-trained generator model.
        pretrainD (str): Path to the pre-trained discriminator model.
        custom_total_epoch (int): The total number of epochs for training.
        custom_save_every_weights (int): The interval (in epochs) at which to save model weights.
        config (object): Configuration object containing training parameters.
        device (torch.device): The device to use for training (CPU or GPU).
    """
    global global_step, smoothed_value_gen, smoothed_value_disc

    smoothed_value_gen = 0
    smoothed_value_disc = 0

    if rank == 0:
        writer_eval = SummaryWriter(log_dir=os.path.join(experiment_dir, "eval"))
    else:
        writer_eval = None

    dist.init_process_group(
        backend="gloo",
        init_method="env://",
        world_size=n_gpus if device.type == "cuda" else 1,
        rank=rank if device.type == "cuda" else 0,
    )

    torch.manual_seed(config.train.seed)

    if torch.cuda.is_available():
        torch.cuda.set_device(device_id)

    # Create datasets and dataloaders
    from data_utils import (
        DistributedBucketSampler,
        TextAudioCollateMultiNSFsid,
        TextAudioLoaderMultiNSFsid,
    )

    train_dataset = TextAudioLoaderMultiNSFsid(config.data)
    collate_fn = TextAudioCollateMultiNSFsid()
    train_sampler = DistributedBucketSampler(
        train_dataset,
        batch_size * n_gpus,
        [50, 100, 200, 300, 400, 500, 600, 700, 800, 900],
        num_replicas=n_gpus,
        rank=rank,
        shuffle=True,
    )

    train_loader = DataLoader(
        train_dataset,
        num_workers=4,
        shuffle=False,
        pin_memory=True,
        collate_fn=collate_fn,
        batch_sampler=train_sampler,
        persistent_workers=True,
        prefetch_factor=8,
    )

    # Initialize models and optimizers
    from rvc.lib.algorithm.discriminators import MultiPeriodDiscriminator
    from rvc.lib.algorithm.synthesizers import Synthesizer

    net_g = Synthesizer(
        config.data.filter_length // 2 + 1,
        config.train.segment_size // config.data.hop_length,
        **config.model,
        use_f0=True,
        is_half=config.train.fp16_run and device.type == "cuda",
        sr=sample_rate,
        vocoder=vocoder,
        checkpointing=checkpointing,
    )
    
    net_d = MultiPeriodDiscriminator(
        version, config.model.use_spectral_norm, checkpointing=checkpointing
    )

    if torch.cuda.is_available():
        net_g = net_g.cuda(device_id)
        net_d = net_d.cuda(device_id)
    else:
        net_g.to(device)
        net_d.to(device)

    optim_g = torch.optim.AdamW(
        net_g.parameters(),
        config.train.learning_rate,
        betas=config.train.betas,
        eps=config.train.eps,
    )
    optim_d = torch.optim.AdamW(
        net_d.parameters(),
        config.train.learning_rate,
        betas=config.train.betas,
        eps=config.train.eps,
    )

    fn_mel_loss = MultiScaleMelSpectrogramLoss(sample_rate=sample_rate)

    # Wrap models with DDP for multi-gpu processing
    if n_gpus > 1 and device.type == "cuda":
        net_g = DDP(net_g, device_ids=[device_id])
        net_d = DDP(net_d, device_ids=[device_id])

    # Load checkpoint if available
    try:
        print("Starting training...")
        _, _, _, epoch_str = load_checkpoint(
            latest_checkpoint_path(experiment_dir, "D_*.pth"), net_d, optim_d
        )
        _, _, _, epoch_str = load_checkpoint(
            latest_checkpoint_path(experiment_dir, "G_*.pth"), net_g, optim_g
        )
        epoch_str += 1
        global_step = (epoch_str - 1) * len(train_loader)

    except:
        epoch_str = 1
        global_step = 0
        if pretrainG != "" and pretrainG != "None":
            if rank == 0:
                verify_checkpoint_shapes(pretrainG, net_g)
                print(f"Loaded pretrained (G) '{pretrainG}'")
            if hasattr(net_g, "module"):
                net_g.module.load_state_dict(
                    torch.load(pretrainG, map_location="cpu")["model"]
                )
            else:
                net_g.load_state_dict(
                    torch.load(pretrainG, map_location="cpu")["model"]
                )

        if pretrainD != "" and pretrainD != "None":
            if rank == 0:
                print(f"Loaded pretrained (D) '{pretrainD}'")
            if hasattr(net_d, "module"):
                net_d.module.load_state_dict(
                    torch.load(pretrainD, map_location="cpu")["model"]
                )
            else:
                net_d.load_state_dict(
                    torch.load(pretrainD, map_location="cpu")["model"]
                )

    # Initialize schedulers and scaler
    scheduler_g = torch.optim.lr_scheduler.ExponentialLR(
        optim_g, gamma=config.train.lr_decay, last_epoch=epoch_str - 2
    )
    scheduler_d = torch.optim.lr_scheduler.ExponentialLR(
        optim_d, gamma=config.train.lr_decay, last_epoch=epoch_str - 2
    )

    scaler = GradScaler(enabled=config.train.fp16_run and device.type == "cuda")

    cache = []
    # get the first sample as reference for tensorboard evaluation
    # custom reference temporarily disabled
    if True == False and os.path.isfile(
        os.path.join("logs", "reference", f"ref{sample_rate}.wav")
    ):
        phone = np.load(
            os.path.join("logs", "reference", f"ref{sample_rate}_feats.npy")
        )
        # expanding x2 to match pitch size
        phone = np.repeat(phone, 2, axis=0)
        phone = torch.FloatTensor(phone).unsqueeze(0).to(device)
        phone_lengths = torch.LongTensor(phone.size(0)).to(device)
        pitch = np.load(os.path.join("logs", "reference", f"ref{sample_rate}_f0c.npy"))
        # removed last frame to match features
        pitch = torch.LongTensor(pitch[:-1]).unsqueeze(0).to(device)
        pitchf = np.load(os.path.join("logs", "reference", f"ref{sample_rate}_f0f.npy"))
        # removed last frame to match features
        pitchf = torch.FloatTensor(pitchf[:-1]).unsqueeze(0).to(device)
        sid = torch.LongTensor([0]).to(device)
        reference = (
            phone,
            phone_lengths,
            pitch,
            pitchf,
            sid,
        )
    else:
        for info in train_loader:
            phone, phone_lengths, pitch, pitchf, _, _, _, _, sid = info
            if device.type == "cuda":
                reference = (
                    phone.cuda(device_id, non_blocking=True),
                    phone_lengths.cuda(device_id, non_blocking=True),
                    pitch.cuda(device_id, non_blocking=True),
                    pitchf.cuda(device_id, non_blocking=True),
                    sid.cuda(device_id, non_blocking=True),
                )            
            else:
                reference = (
                    phone.to(device),
                    phone_lengths.to(device),
                    pitch.to(device),
                    pitchf.to(device),
                    sid.to(device),
                )
            break

    for epoch in range(epoch_str, total_epoch + 1):
        train_and_evaluate(
            rank,
            epoch,
            config,
            [net_g, net_d],
            [optim_g, optim_d],
            scaler,
            [train_loader, None],
            [writer_eval],
            cache,
            custom_save_every_weights,
            custom_total_epoch,
            device,
            device_id,
            reference,
            fn_mel_loss,
        )

        scheduler_g.step()
        scheduler_d.step()


def train_and_evaluate(
    rank,
    epoch,
    hps,
    nets,
    optims,
    scaler,
    loaders,
    writers,
    cache,
    custom_save_every_weights,
    custom_total_epoch,
    device,
    device_id,
    reference,
    fn_mel_loss,
):
    """
    Trains and evaluates the model for one epoch.

    Args:
        rank (int): Rank of the current process.
        epoch (int): Current epoch number.
        hps (Namespace): Hyperparameters.
        nets (list): List of models [net_g, net_d].
        optims (list): List of optimizers [optim_g, optim_d].
        scaler (GradScaler): Gradient scaler for mixed precision training.
        loaders (list): List of dataloaders [train_loader, eval_loader].
        writers (list): List of TensorBoard writers [writer_eval].
        cache (list): List to cache data in GPU memory.
        use_cpu (bool): Whether to use CPU for training.
    """
    global global_step, lowest_value, loss_disc, consecutive_increases_gen, consecutive_increases_disc, smoothed_value_gen, smoothed_value_disc

    if epoch == 1:
        lowest_value = {"step": 0, "value": float("inf"), "epoch": 0}
        consecutive_increases_gen = 0
        consecutive_increases_disc = 0

    epoch_disc_sum = 0.0
    epoch_gen_sum = 0.0

    net_g, net_d = nets
    optim_g, optim_d = optims
    train_loader = loaders[0] if loaders is not None else None
    if writers is not None:
        writer = writers[0]

    train_loader.batch_sampler.set_epoch(epoch)

    net_g.train()
    net_d.train()

    # Data caching
    if device.type == "cuda" and cache_data_in_gpu:
        data_iterator = cache
        if cache == []:
            for batch_idx, info in enumerate(train_loader):
                # phone, phone_lengths, pitch, pitchf, spec, spec_lengths, wave, wave_lengths, sid
                info = [tensor.cuda(device_id, non_blocking=True) for tensor in info]
                cache.append((batch_idx, info))
        else:
            shuffle(cache)
    else:
        data_iterator = enumerate(train_loader)

    epoch_recorder = EpochRecorder()
    with tqdm(total=len(train_loader), leave=False) as pbar:
        for batch_idx, info in data_iterator:
            if device.type == "cuda" and not cache_data_in_gpu:
                info = [tensor.cuda(device_id, non_blocking=True) for tensor in info]
            elif device.type != "cuda":
                info = [tensor.to(device) for tensor in info]
            # else iterator is going thru a cached list with a device already assigned

            (
                phone,
                phone_lengths,
                pitch,
                pitchf,
                spec,
                spec_lengths,
                wave,
                wave_lengths,
                sid,
            ) = info

            # Forward pass
            use_amp = config.train.fp16_run and device.type == "cuda"
            with autocast(enabled=use_amp):
                model_output = net_g(
                    phone, phone_lengths, pitch, pitchf, spec, spec_lengths, sid
                )
                y_hat, ids_slice, x_mask, z_mask, (z, z_p, m_p, logs_p, m_q, logs_q) = (
                    model_output
                )
                # slice of the original waveform to match a generate slice
                wave = commons.slice_segments(
                    wave,
                    ids_slice * config.data.hop_length,
                    config.train.segment_size,
                    dim=3,
                )
                y_d_hat_r, y_d_hat_g, _, _ = net_d(wave, y_hat.detach())
                with autocast(enabled=False):
                    # if vocoder == "HiFi-GAN":
                    #    loss_disc, _, _ = discriminator_loss(y_d_hat_r, y_d_hat_g)
                    # else:
                    #    loss_disc, _, _ = discriminator_loss_scaled(y_d_hat_r, y_d_hat_g)
                    loss_disc, _, _ = discriminator_loss(y_d_hat_r, y_d_hat_g)
            # Discriminator backward and update
            epoch_disc_sum += loss_disc
            optim_d.zero_grad()
            scaler.scale(loss_disc).backward()
            scaler.unscale_(optim_d)
            grad_norm_d = torch.nn.utils.clip_grad_norm_(
                net_d.parameters(), max_norm=1000.0
            )
            scaler.step(optim_d)
            scaler.update()
            # if not math.isfinite(grad_norm_d):
            #    print("\nWarning: grad_norm_d is NaN or Inf")

            # Generator backward and update
            with autocast(enabled=use_amp):
                _, y_d_hat_g, fmap_r, fmap_g = net_d(wave, y_hat)
                with autocast(enabled=False):
                    loss_mel = fn_mel_loss(wave, y_hat) * config.train.c_mel / 3.0
                    loss_env = envelope_loss(wave, y_hat)
                    loss_kl = (
                        kl_loss(z_p, logs_q, m_p, logs_p, z_mask) * config.train.c_kl
                    )
                    loss_fm = feature_loss(fmap_r, fmap_g)
                    # if vocoder == "HiFi-GAN":
                    # 	loss_gen, _ = generator_loss(y_d_hat_g)
                    # else:
                    # 	loss_gen, _ = generator_loss_scaled(y_d_hat_g)
                    loss_gen, _ = generator_loss(y_d_hat_g)
                    loss_gen_all = loss_gen + loss_fm + loss_mel + loss_kl + loss_env

                    if loss_gen_all < lowest_value["value"]:
                        lowest_value = {
                            "step": global_step,
                            "value": loss_gen_all,
                            "epoch": epoch,
                        }
            epoch_gen_sum += loss_gen_all
            optim_g.zero_grad()
            scaler.scale(loss_gen_all).backward()
            scaler.unscale_(optim_g)
            grad_norm_g = torch.nn.utils.clip_grad_norm_(
                net_g.parameters(), max_norm=1000.0
            )
            scaler.step(optim_g)
            scaler.update()
            # if not math.isfinite(grad_norm_g):
            #    print("\n Warning: grad_norm_g is NaN or Inf")

            global_step += 1

            # queue for rolling losses over 50 steps
            avg_losses["disc_loss_50"].append(loss_disc.detach())
            avg_losses["env_loss_50"].append(loss_env.detach())
            avg_losses["fm_loss_50"].append(loss_fm.detach())
            avg_losses["kl_loss_50"].append(loss_kl.detach())
            avg_losses["mel_loss_50"].append(loss_mel.detach())
            avg_losses["gen_loss_50"].append(loss_gen_all.detach())

            if rank == 0 and global_step % 50 == 0:
                # logging rolling averages
                scalar_dict = {
                    "loss_avg_50/d/total": torch.mean(
                        torch.stack(list(avg_losses["disc_loss_50"]))
                    ),
                    "loss_avg_50/g/env": torch.mean(
                        torch.stack(list(avg_losses["env_loss_50"]))
                    ),
                    "loss_avg_50/g/fm": torch.mean(
                        torch.stack(list(avg_losses["fm_loss_50"]))
                    ),
                    "loss_avg_50/g/kl": torch.mean(
                        torch.stack(list(avg_losses["kl_loss_50"]))
                    ),
                    "loss_avg_50/g/mel": torch.mean(
                        torch.stack(list(avg_losses["mel_loss_50"]))
                    ),
                    "loss_avg_50/g/total": torch.mean(
                        torch.stack(list(avg_losses["gen_loss_50"]))
                    ),
                }
                summarize(
                    writer=writer,
                    global_step=global_step,
                    scalars=scalar_dict,
                )

            pbar.update(1)

    with torch.no_grad():
        torch.cuda.empty_cache()

    # Logging and checkpointing
    if rank == 0:

        avg_losses["disc_loss_queue"].append(epoch_disc_sum.item() / len(train_loader))
        avg_losses["gen_loss_queue"].append(epoch_gen_sum.item() / len(train_loader))

        # used for tensorboard chart - all/mel
        mel = spec_to_mel_torch(
            spec,
            config.data.filter_length,
            config.data.n_mel_channels,
            config.data.sample_rate,
            config.data.mel_fmin,
            config.data.mel_fmax,
        )
        # used for tensorboard chart - slice/mel_org
        y_mel = commons.slice_segments(
            mel,
            ids_slice,
            config.train.segment_size // config.data.hop_length,
            dim=3,
        )
        # used for tensorboard chart - slice/mel_gen
        with autocast(enabled=False):
            y_hat_mel = mel_spectrogram_torch(
                y_hat.float().squeeze(1),
                config.data.filter_length,
                config.data.n_mel_channels,
                config.data.sample_rate,
                config.data.hop_length,
                config.data.win_length,
                config.data.mel_fmin,
                config.data.mel_fmax,
            )
            if use_amp:
                y_hat_mel = y_hat_mel.half()

        lr = optim_g.param_groups[0]["lr"]

        scalar_dict = {
            "loss/g/total": loss_gen_all,
            "loss/d/total": loss_disc,
            "learning_rate": lr,
            "grad/norm_d": grad_norm_d.item(),
            "grad/norm_g": grad_norm_g.item(),
            "loss/g/fm": loss_fm,
            "loss/g/mel": loss_mel,
            "loss/g/kl": loss_kl,
            "loss/g/env": loss_env,
            "loss_avg_epoch/disc": np.mean(avg_losses["disc_loss_queue"]),
            "loss_avg_epoch/gen": np.mean(avg_losses["gen_loss_queue"]),
        }
        # commented out
        # scalar_dict.update({f"loss/g/{i}": v for i, v in enumerate(losses_gen)})
        # scalar_dict.update({f"loss/d_r/{i}": v for i, v in enumerate(losses_disc_r)})
        # scalar_dict.update({f"loss/d_g/{i}": v for i, v in enumerate(losses_disc_g)})

        image_dict = {
            "slice/mel_org": plot_spectrogram_to_numpy(y_mel[0].data.cpu().numpy()),
            "slice/mel_gen": plot_spectrogram_to_numpy(y_hat_mel[0].data.cpu().numpy()),
            "all/mel": plot_spectrogram_to_numpy(mel[0].data.cpu().numpy()),
        }

        if epoch % save_every_epoch == 0:
            with torch.no_grad():
                if hasattr(net_g, "module"):
                    o, *_ = net_g.module.infer(*reference)
                else:
                    o, *_ = net_g.infer(*reference)
            audio_dict = {f"gen/audio_{global_step:07d}": o[0, :, :]}
            summarize(
                writer=writer,
                global_step=global_step,
                images=image_dict,
                scalars=scalar_dict,
                audios=audio_dict,
                audio_sample_rate=config.data.sample_rate,
            )
        else:
            summarize(
                writer=writer,
                global_step=global_step,
                images=image_dict,
                scalars=scalar_dict,
            )

    # Save checkpoint
    model_add = []
    model_del = []
    done = False

    if rank == 0:
        overtrain_info = ""
        # Check overtraining
        if overtraining_detector and rank == 0 and epoch > 1:
            # Add the current loss to the history
            current_loss_disc = float(loss_disc)
            loss_disc_history.append(current_loss_disc)
            # Update smoothed loss history with loss_disc
            smoothed_value_disc = update_exponential_moving_average(
                smoothed_loss_disc_history, current_loss_disc
            )
            # Check overtraining with smoothed loss_disc
            is_overtraining_disc = check_overtraining(
                smoothed_loss_disc_history, overtraining_threshold * 2
            )
            if is_overtraining_disc:
                consecutive_increases_disc += 1
            else:
                consecutive_increases_disc = 0
            # Add the current loss_gen to the history
            current_loss_gen = float(lowest_value["value"])
            loss_gen_history.append(current_loss_gen)
            # Update the smoothed loss_gen history
            smoothed_value_gen = update_exponential_moving_average(
                smoothed_loss_gen_history, current_loss_gen
            )
            # Check for overtraining with the smoothed loss_gen
            is_overtraining_gen = check_overtraining(
                smoothed_loss_gen_history, overtraining_threshold, 0.01
            )
            if is_overtraining_gen:
                consecutive_increases_gen += 1
            else:
                consecutive_increases_gen = 0
            overtrain_info = f"Smoothed loss_g {smoothed_value_gen:.3f} and loss_d {smoothed_value_disc:.3f}"
            # Save the data in the JSON file if the epoch is divisible by save_every_epoch
            if epoch % save_every_epoch == 0:
                save_to_json(
                    training_file_path,
                    loss_disc_history,
                    smoothed_loss_disc_history,
                    loss_gen_history,
                    smoothed_loss_gen_history,
                )

            if (
                is_overtraining_gen
                and consecutive_increases_gen == overtraining_threshold
                or is_overtraining_disc
                and consecutive_increases_disc == overtraining_threshold * 2
            ):
                print(
                    f"Overtraining detected at epoch {epoch} with smoothed loss_g {smoothed_value_gen:.3f} and loss_d {smoothed_value_disc:.3f}"
                )
                done = True
            else:
                print(
                    f"New best epoch {epoch} with smoothed loss_g {smoothed_value_gen:.3f} and loss_d {smoothed_value_disc:.3f}"
                )
                old_model_files = glob.glob(
                    os.path.join(experiment_dir, f"{model_name}_*e_*s_best_epoch.pth")
                )
                for file in old_model_files:
                    model_del.append(file)
                model_add.append(
                    os.path.join(
                        experiment_dir,
                        f"{model_name}_{epoch}e_{global_step}s_best_epoch.pth",
                    )
                )

        # Print training progress
        lowest_value_rounded = float(lowest_value["value"])
        lowest_value_rounded = round(lowest_value_rounded, 3)

        record = f"{model_name} | epoch={epoch} | step={global_step} | {epoch_recorder.record()}"
        if epoch > 1:
            record = (
                record
                + f" | lowest_value={lowest_value_rounded} (epoch {lowest_value['epoch']} and step {lowest_value['step']})"
            )

        if overtraining_detector:
            remaining_epochs_gen = overtraining_threshold - consecutive_increases_gen
            remaining_epochs_disc = (
                overtraining_threshold * 2 - consecutive_increases_disc
            )
            record = (
                record
                + f" | Number of epochs remaining for overtraining: g/total: {remaining_epochs_gen} d/total: {remaining_epochs_disc} | smoothed_loss_gen={smoothed_value_gen:.3f} | smoothed_loss_disc={smoothed_value_disc:.3f}"
            )
        print(record)

        # Save weights every N epochs
        if epoch % save_every_epoch == 0:
            checkpoint_suffix = f"{2333333 if save_only_latest else global_step}.pth"
            save_checkpoint(
                net_g,
                optim_g,
                config.train.learning_rate,
                epoch,
                os.path.join(experiment_dir, "G_" + checkpoint_suffix),
            )
            save_checkpoint(
                net_d,
                optim_d,
                config.train.learning_rate,
                epoch,
                os.path.join(experiment_dir, "D_" + checkpoint_suffix),
            )
            if custom_save_every_weights:
                model_add.append(
                    os.path.join(
                        experiment_dir, f"{model_name}_{epoch}e_{global_step}s.pth"
                    )
                )

        if model_add:
            ckpt = (
                net_g.module.state_dict()
                if hasattr(net_g, "module")
                else net_g.state_dict()
            )
            for m in model_add:
                if not os.path.exists(m):
                    extract_model(
                        ckpt=ckpt,
                        sr=sample_rate,
                        pitch_guidance=True,
                        name=model_name,
                        model_dir=m,
                        epoch=epoch,
                        step=global_step,
                        version=version,
                        hps=hps,
                        overtrain_info=overtrain_info,
                        vocoder=vocoder,
                    )
        # Clean-up old best epochs
        for m in model_del:
            os.remove(m)

        # Check completion
        if epoch >= custom_total_epoch:
            lowest_value_rounded = float(lowest_value["value"])
            lowest_value_rounded = round(lowest_value_rounded, 3)
            print(
                f"Training has been successfully completed with {epoch} epoch, {global_step} steps and {round(loss_gen_all.item(), 3)} loss gen."
            )
            print(
                f"Lowest generator loss: {lowest_value_rounded} at epoch {lowest_value['epoch']}, step {lowest_value['step']}"
            )

            pid_file_path = os.path.join(experiment_dir, "config.json")
            with open(pid_file_path, "r") as pid_file:
                pid_data = json.load(pid_file)
            with open(pid_file_path, "w") as pid_file:
                pid_data.pop("process_pids", None)
                json.dump(pid_data, pid_file, indent=4)
            # Final model
            model_add.append(
                os.path.join(
                    experiment_dir, f"{model_name}_{epoch}e_{global_step}s.pth"
                )
            )
            done = True

        if done:
            os._exit(2333333)

        with torch.no_grad():
            torch.cuda.empty_cache()


def check_overtraining(smoothed_loss_history, threshold, epsilon=0.004):
    """
    Checks for overtraining based on the smoothed loss history.

    Args:
        smoothed_loss_history (list): List of smoothed losses for each epoch.
        threshold (int): Number of consecutive epochs with insignificant changes or increases to consider overtraining.
        epsilon (float): The maximum change considered insignificant.
    """
    if len(smoothed_loss_history) < threshold + 1:
        return False

    for i in range(-threshold, -1):
        if smoothed_loss_history[i + 1] > smoothed_loss_history[i]:
            return True
        if abs(smoothed_loss_history[i + 1] - smoothed_loss_history[i]) >= epsilon:
            return False
    return True


def update_exponential_moving_average(
    smoothed_loss_history, new_value, smoothing=0.987
):
    """
    Updates the exponential moving average with a new value.

    Args:
        smoothed_loss_history (list): List of smoothed values.
        new_value (float): New value to be added.
        smoothing (float): Smoothing factor.
    """
    if smoothed_loss_history:
        smoothed_value = (
            smoothing * smoothed_loss_history[-1] + (1 - smoothing) * new_value
        )
    else:
        smoothed_value = new_value
    smoothed_loss_history.append(smoothed_value)
    return smoothed_value


def save_to_json(
    file_path,
    loss_disc_history,
    smoothed_loss_disc_history,
    loss_gen_history,
    smoothed_loss_gen_history,
):
    """
    Save the training history to a JSON file.
    """
    data = {
        "loss_disc_history": loss_disc_history,
        "smoothed_loss_disc_history": smoothed_loss_disc_history,
        "loss_gen_history": loss_gen_history,
        "smoothed_loss_gen_history": smoothed_loss_gen_history,
    }
    with open(file_path, "w") as f:
        json.dump(data, f)


if __name__ == "__main__":
    torch.multiprocessing.set_start_method("spawn")
    main()
