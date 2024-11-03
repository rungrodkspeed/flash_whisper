import re
import time
import json
import torch
import numpy as np

from .fbank import FeatureExtractor
from .tokenizer import get_tokenizer

from pathlib import Path
from typing import List, Union
from collections import OrderedDict
from tensorrt_llm.runtime import ModelRunnerCpp
from tensorrt_llm.bindings import GptJsonConfig


def read_config(component, engine_dir):
    config_path = engine_dir / component / 'config.json'
    with open(config_path, 'r') as f:
        config = json.load(f)
    model_config = OrderedDict()
    model_config.update(config['pretrained_config'])
    model_config.update(config['build_config'])
    return model_config

class WhisperTRTLLM:
    
    def __init__(self, engine_dir:str, n_mels:int = 128, zero_pad:bool = False):
        
        json_config = GptJsonConfig.parse_file(Path(engine_dir) / 'decoder' / 'config.json')
        assert json_config.model_config.supports_inflight_batching
        runner_kwargs = dict(
            engine_dir=engine_dir,
            is_enc_dec=True,
            max_batch_size=8,
            max_input_len=3000,
            max_output_len=96,
            max_beam_width=1,
            debug_mode=False,
            kv_cache_free_gpu_memory_fraction=0.5
        )
        
        self.model_runner_cpp = ModelRunnerCpp.from_dir(**runner_kwargs)
        self.feature_extractor = FeatureExtractor(n_mels=n_mels)
        self.zero_pad = zero_pad
        self.eot_id = 50257
        
        encoder_config = read_config('encoder', Path(engine_dir))
        self.tokenizer = get_tokenizer(num_languages=encoder_config['num_languages'])
        self.blank = self.tokenizer.encode(" ", allowed_special=self.tokenizer.special_tokens_set)[0]
        self.device = torch.device("cuda")
        
    def __call__(
        self,
        wav:Union[np.ndarray, List[np.ndarray]],
        wav_length:Union[np.ndarray, List],
        prompt_ids:Union[str, List[str]] = "<|startoftranscript|><|en|><|transcribe|><|notimestamps|>"
    ):
        start_time = time.time()
        prompt_ids = self.tokenizer.encode(prompt_ids, allowed_special=self.tokenizer.special_tokens_set)
        print(f"token encode : {time.time() - start_time}")
        
        start_time = time.time()
        output_ids = self.process_batch(wav, wav_length, prompt_ids)
        print(f"process batch : {time.time() - start_time}")
        
        start_time = time.time()
        s = [re.sub(r'<\|.*?\|>', '', self.tokenizer.decode(output_id)) for output_id in output_ids]
        print(f"token decode : {time.time() - start_time}")
        
        return s
    
    def process_batch(
        self,
        waves,
        wav_lengths,
        prompt_ids,
    ):
        batch_mel_list, decoder_input_ids = [], []
        for wav, wav_len in zip(waves, wav_lengths):
            
            start_time = time.time()
            wav = torch.from_numpy(wav).to(self.device)
            prompt_ids = torch.tensor(prompt_ids).unsqueeze(0)

            wav = wav[:wav_len]
            padding = 0 if self.zero_pad else 3000
            print(f"\t code sec 1 : {time.time() - start_time}")
            
            start_time = time.time()
            mel = self.feature_extractor.compute_feature(wav.to('cuda'), padding_target_len=padding).transpose(1, 2)
            print(f"\t mel extract : {time.time() - start_time}")
            
            batch_mel_list.append(mel.squeeze(0))
            decoder_input_ids.append(prompt_ids.int().to("cuda").squeeze(0))
        
        start_time = time.time()
        decoder_input_ids = torch.nn.utils.rnn.pad_sequence(decoder_input_ids, batch_first=True, padding_value=self.eot_id)
        print(f"torch pad_sequence : {time.time() - start_time}")
        
        mel_input_lengths = torch.tensor([mel.shape[0] for mel in batch_mel_list], dtype=torch.int32, device='cuda')

        start_time=time.time()
        outputs = self.model_runner_cpp.generate(
            batch_input_ids=decoder_input_ids,
            encoder_input_features=batch_mel_list,
            encoder_output_lengths=mel_input_lengths // 2,
            max_new_tokens=96,
            end_id=self.eot_id,
            pad_id=self.eot_id,
            num_beams=1,
            output_sequence_lengths=True,
            return_dict=True)
        
        torch.cuda.synchronize()
        
        print(f"inference time: {time.time() - start_time}")
        
        output_ids = outputs['output_ids'][0].cpu().numpy()
        
        return output_ids