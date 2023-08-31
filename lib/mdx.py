import hashlib
import warnings

import numpy as np
import torch
from tqdm import tqdm

from webui_utils import gc_collect
import onnxruntime as ort
print(ort.get_available_providers())

warnings.filterwarnings("ignore")
stem_naming = {'Vocals': 'Instrumental', 'Other': 'Instruments', 'Instrumental': 'Vocals', 'Drums': 'Drumless', 'Bass': 'Bassless'}


class MDXModel:
    def __init__(self, device, dim_f, dim_t, n_fft, hop=1024, stem_name=None, compensation=1.000):
        self.dim_f = dim_f
        self.dim_t = dim_t
        self.dim_c = 4
        self.n_fft = n_fft
        self.hop = hop
        self.stem_name = stem_name
        self.compensation = compensation

        self.n_bins = self.n_fft // 2 + 1
        self.chunk_size = hop * (self.dim_t - 1)
        self.device = device
        self.window = torch.hann_window(window_length=self.n_fft, periodic=True,dtype=torch.float32,device=self.device)

        out_c = self.dim_c

        self.freq_pad = torch.zeros([1, out_c, self.n_bins - self.dim_f, self.dim_t]).cpu()

    def stft(self, x):
        x = x.reshape([-1, self.chunk_size])
        x = torch.stft(x, n_fft=self.n_fft, hop_length=self.hop, window=self.window, center=True, return_complex=True)
        x = torch.view_as_real(x)
        x = x.permute([0, 3, 1, 2])
        x = x.reshape([-1, 2, 2, self.n_bins, self.dim_t]).reshape([-1, 4, self.n_bins, self.dim_t])
        return x[:, :, :self.dim_f]

    def istft(self, x, freq_pad=None):
        freq_pad = self.freq_pad.repeat([x.shape[0], 1, 1, 1]) if freq_pad is None else freq_pad
        x = torch.cat([x, freq_pad], -2)
        # c = 4*2 if self.target_name=='*' else 2
        x = x.reshape([-1, 2, 2, self.n_bins, self.dim_t]).reshape([-1, 2, self.n_bins, self.dim_t])
        x = x.permute([0, 2, 3, 1])
        x = x.contiguous()
        x = torch.view_as_complex(x)
        x = torch.istft(x.to(self.device), n_fft=self.n_fft, hop_length=self.hop, window=self.window, center=True)
        return x.reshape([-1, 2, self.chunk_size])


class MDX:
    DEFAULT_SR = 44100
    # Unit: seconds
    DEFAULT_CHUNK_SIZE = 0 * DEFAULT_SR
    DEFAULT_MARGIN_SIZE = 1 * DEFAULT_SR

    DEFAULT_PROCESSOR = 0

    # def __init__(self, model_path: str, params: MDXModel, processor=DEFAULT_PROCESSOR,margin=DEFAULT_MARGIN_SIZE,chunks=15):
    def __init__(self, model_path: str, params: MDXModel, device="cpu",margin=DEFAULT_MARGIN_SIZE,chunks=15):

        # Set the device and the provider (CPU or CUDA)
        self.device = device #torch.device(f'cuda:{processor}') if processor >= 0 else torch.device('cpu')
        # self.provider = ['CUDAExecutionProvider'] if processor >= 0 else ['CPUExecutionProvider']
        self.providers = ['CUDAExecutionProvider','CPUExecutionProvider']

        self.model = params

        # Load the ONNX model using ONNX Runtime
        self.ort = ort.InferenceSession(model_path, providers=self.providers)
        # print(self.ort,self.device,self.providers)

        # Preload the model for faster performance
        # self.ort.run(None, {'input': torch.rand(1, 4, params.dim_f, params.dim_t).numpy()})
        self.process = lambda spec: self.ort.run(None, {'input': spec.cpu().numpy()})[0]
        self.margin=margin
        self.chunks=chunks

        # self.prog = None

    def __del__(self):
        del self.ort
        gc_collect()

    @staticmethod
    def get_hash(model_path):
        try:
            with open(model_path, 'rb') as f:
                f.seek(- 10000 * 1024, 2)
                model_hash = hashlib.md5(f.read()).hexdigest()
        except:
            model_hash = hashlib.md5(open(model_path, 'rb').read()).hexdigest()

        return model_hash

    @staticmethod
    def segment(wave, combine=True, chunk_size=DEFAULT_CHUNK_SIZE, margin_size=DEFAULT_MARGIN_SIZE):
        """
        Segment or join segmented wave array

        Args:
            wave: (np.array) Wave array to be segmented or joined
            combine: (bool) If True, combines segmented wave array. If False, segments wave array.
            chunk_size: (int) Size of each segment (in samples)
            margin_size: (int) Size of margin between segments (in samples)

        Returns:
            numpy array: Segmented or joined wave array
        """

        if combine:
            processed_wave = None  # Initializing as None instead of [] for later numpy array concatenation
            for segment_count, segment in enumerate(wave):
                start = 0 if segment_count == 0 else margin_size
                end = None if segment_count == len(wave) - 1 else -margin_size
                if margin_size == 0:
                    end = None
                if processed_wave is None:  # Create array for first segment
                    processed_wave = segment[:, start:end]
                else:  # Concatenate to existing array for subsequent segments
                    processed_wave = np.concatenate((processed_wave, segment[:, start:end]), axis=-1)

        else:
            processed_wave = []
            sample_count = wave.shape[-1]

            if chunk_size <= 0 or chunk_size > sample_count:
                chunk_size = sample_count

            if margin_size > chunk_size:
                margin_size = chunk_size

            for segment_count, skip in enumerate(range(0, sample_count, chunk_size)):

                margin = 0 if segment_count == 0 else margin_size
                end = min(skip + chunk_size + margin_size, sample_count)
                start = skip - margin

                cut = wave[:, start:end].copy()
                processed_wave.append(cut)

                if end == sample_count:
                    break

        return processed_wave

    def pad_wave(self, wave):
        """
        Pad the wave array to match the required chunk size

        Args:
            wave: (np.array) Wave array to be padded

        Returns:
            tuple: (padded_wave, pad, trim)
                - padded_wave: Padded wave array
                - pad: Number of samples that were padded
                - trim: Number of samples that were trimmed
        """
        n_sample = wave.shape[1]
        trim = self.model.n_fft // 2
        gen_size = self.model.chunk_size - 2 * trim
        pad = gen_size - n_sample % gen_size

        # Padded wave
        wave_p = np.concatenate((np.zeros((2, trim)), wave, np.zeros((2, pad)), np.zeros((2, trim))), 1)

        mix_waves = []
        for i in range(0, n_sample + pad, gen_size):
            waves = np.array(wave_p[:, i:i + self.model.chunk_size])
            mix_waves.append(waves)

        mix_waves = torch.tensor(mix_waves, dtype=torch.float32).cpu()

        return mix_waves, pad, trim

    # def _process_wave(self, mix_waves, trim, pad, q: queue.Queue, _id: int):
    def _process_wave(self, mix_waves, trim, pad):
        """
        Process each wave segment in a multi-threaded environment

        Args:
            mix_waves: (torch.Tensor) Wave segments to be processed
            trim: (int) Number of samples trimmed during padding
            pad: (int) Number of samples padded during padding
            q: (queue.Queue) Queue to hold the processed wave segments
            _id: (int) Identifier of the processed wave segment

        Returns:
            numpy array: Processed wave segment
        """
        mix_waves = mix_waves.split(1)
        with torch.no_grad():
            pw = []
            for mix_wave in mix_waves:
                # self.prog.update()
                spec = self.model.stft(mix_wave.to(self.device))
                processed_spec = torch.tensor(self.process(spec))
                processed_wav = self.model.istft(processed_spec)
                processed_wav = processed_wav[:, :, trim:-trim].transpose(0, 1).reshape(2, -1).cpu().numpy()
                pw.append(processed_wav)

        processed_signal = np.concatenate(pw, axis=-1)[:, :-pad]
        del pw, mix_waves, processed_wav
        gc_collect()
        
        return processed_signal

    def process_wave(self, wave: np.array, mt_threads=1):
        """
        Process the wave array in a multi-threaded environment

        Args:
            wave: (np.array) Wave array to be processed
            mt_threads: (int) Number of threads to be used for processing

        Returns:
            numpy array: Processed wave array
        """
        # self.prog = tqdm(total=0)
        # chunk_size = wave.shape[-1] // (mt_threads if not self.chunks else (self.chunks*mt_threads))
        chunk_size = self.model.chunk_size
        margin = self.margin
        waves = self.segment(wave, False, chunk_size, margin)
        
        processed_batches = []
        for batch in tqdm(waves,desc="processing waves"):
            mix_waves, pad, trim = self.pad_wave(batch)
            processed_waves = self._process_wave(mix_waves, trim, pad)
            processed_batches.append(processed_waves)

        assert len(processed_batches) == len(waves), 'Incomplete processed batches, please reduce batch size!'
        segmented_mix = self.segment(processed_batches, True, chunk_size, margin)
        del processed_batches, processed_waves, mix_waves
        gc_collect()
        return segmented_mix