import copy
import logging
import numpy as np

from typing import List, Dict, Optional, Union
from onnxruntime import InferenceSession

from .base import ORTModelBase
from ..config.generation_config import GenerationConfig
from ..whisper.feature_extractor import WhisperFeatureExtractor
from ..tokenizer.tokenizer_whisper import WhisperTokenizer, TO_LANGUAGE_CODE
from ..whisper.criteria import StoppingCriteriaList, MaxLengthCriteria, EosTokenCriteria

logger = logging.getLogger(__name__)

class ORTEncoder(ORTModelBase):
    
    def __init__(self, session: InferenceSession):
        super().__init__(session)
    
    def forward(self, inputs: List[np.ndarray], **kwargs) -> Dict[str, np.ndarray]:
        out = self.session.run(None, self.binding_inputs(inputs))
        return self.binding_outputs(out)
    
class ORTWhisper:
    
    encoder: ORTEncoder
    tokenizer: WhisperTokenizer
    generation_config: GenerationConfig
    feature_extractor: WhisperFeatureExtractor
    
    def __init__(self, encoder:InferenceSession):
        self.encoder = ORTEncoder(encoder)
        self.tokenizer = WhisperTokenizer()
        self.generation_config = GenerationConfig()
        self.feature_extractor = WhisperFeatureExtractor()
    
    def __call__(self, 
                 audio:np.ndarray, 
                 sampling_rate:int, 
                 language: Optional[Union[str, List[str]]] = None,
                 **kwargs):
        
        input_features, stopping_criteria, model_kwargs = self.preprocess(audio=audio, 
                                                                          sampling_rate=sampling_rate, 
                                                                          language=language)
        
        out = self.encoder([input_features])
        return out
    
    def preprocess(self, 
                 audio:np.ndarray, 
                 sampling_rate:int, 
                 language: Optional[Union[str, List[str]]] = None):
        
        input_features = self.feature_extractor(audio, sampling_rate=sampling_rate)["input_features"]
        
        if language is not None:
            if not hasattr(self.generation_config, "lang_to_id"):
                raise ValueError(
                    "The generation config is outdated and is thus not compatible with the `language` argument "
                    "to `generate`. Either set the language using the `forced_decoder_ids` in the model config, "
                    "or update the generation config as per the instructions https://github.com/huggingface/transformers/issues/25084#issuecomment-1664398224"
                )
            self.generation_config.language = language

        batch_size = input_features.shape[0]
        decoder_input_ids = self._retrieve_init_token(batch_size)

        stopping_criteria = StoppingCriteriaList()
        if self.generation_config.max_length is not None:
            max_position_embeddings = getattr(self.generation_config, "max_position_embeddings", None)
            stopping_criteria.append(
                MaxLengthCriteria(
                    max_length=self.generation_config.max_length,
                    max_position_embeddings=max_position_embeddings,
                )
            )
        if self.generation_config.eos_token_id is not None:
            stopping_criteria.append(EosTokenCriteria(eos_token_id=self.generation_config.eos_token_id))

        return input_features, stopping_criteria, decoder_input_ids
    
    def _retrieve_init_token(self, batch_size:int) -> np.ndarray:
        
        def language_to_id(language: str) -> int:
            language = language.lower()
            if language in self.generation_config.lang_to_id.keys():
                language_token = language
            elif language in TO_LANGUAGE_CODE.keys():
                language_token = f"<|{TO_LANGUAGE_CODE[language]}|>"
            elif language in TO_LANGUAGE_CODE.values():
                language_token = f"<|{language}|>"
            else:
                is_language_code = len(language) == 2
                raise ValueError(
                    f"Unsupported language: {language}. Language should be one of:"
                    f" {list(TO_LANGUAGE_CODE.values()) if is_language_code else list(TO_LANGUAGE_CODE.keys())}."
                )
            if language_token not in self.generation_config.lang_to_id:
                raise ValueError(
                    f"{language_token} is not supported by this specific model as it is not in the `generation_config.lang_to_id`."
                    "(You should just add it to the generation config)"
                )

            return self.generation_config.lang_to_id[language_token]

        language = getattr(self.generation_config, "language", None)
        forced_decoder_ids = self.generation_config.forced_decoder_ids
        if forced_decoder_ids is not None and language is not None:
            logger.warning(
                f"You have passed language={language}, but also have set `forced_decoder_ids` to {forced_decoder_ids} which creates a conflict. `forced_decoder_ids` will be ignored in favor of language={language}."
            )
            forced_decoder_ids = None

        init_tokens = [self.generation_config.decoder_start_token_id]
        if forced_decoder_ids is not None and forced_decoder_ids[0][0] == 1:
            i = 1
            while len(forced_decoder_ids) > 0 and forced_decoder_ids[0][0] == i:
                init_tokens += [forced_decoder_ids[0][1]]
                forced_decoder_ids = forced_decoder_ids[1:]
                i += 1

            if len(forced_decoder_ids) > 0:
                raise ValueError(
                    f"You are using token ids in `forced_decoder_ids` that do not seem to correctly follow the prompt pattern of Whisper. Make sure that {forced_decoder_ids} has an entry for all indices >= 1 and < {forced_decoder_ids[0][0]}.",
                )
                
        self.generation_config.forced_decoder_ids = None

        is_lang_id_undefined = len(init_tokens) <= 1 or (len(init_tokens) > 1 and init_tokens[1] is None)
        if isinstance(language, (list, tuple)):
            if any(l is None for l in language):
                raise TypeError(
                    "Expected `language` to be `None`, a single string (e.g. `'en'`), or a list of strings with length equal to the batch size (e.g. `('en', 'fr')` for a batch size of 2). Got a list containing `None`."
                )
            if len(language) != batch_size:
                raise ValueError(
                    "When passing a list of languages, the length of the list must match the batch size. "
                    f"Expected length of {batch_size}, but got {len(language)} languages."
                )
            languages = language
        elif language is None:
            languages = [None] * batch_size
        else:
            languages = [language]

        init_tokens = [copy.copy(init_tokens) for _ in languages]
        if language is not None:
            lang_ids = [language_to_id(l) for l in languages]
        elif is_lang_id_undefined:
            lang_ids = [language_to_id("en")] * batch_size

        for i in range(len(init_tokens)):
            if len(init_tokens[i]) > 1:
                init_tokens[i][1] = lang_ids[i]
            else:
                init_tokens[i].append(lang_ids[i])
        del languages

        for i in range(len(init_tokens)):
            if language is not None and hasattr(self.generation_config, "task_to_id"):
                # if language is defined, but no task id is in `init_tokens`, default to transcribe
                if not any(ti in init_tokens[i] for ti in self.eneration_config.task_to_id.values()):
                    init_tokens[i].append(self.generation_config.task_to_id["transcribe"])

            if (
                not self.generation_config.return_timestamps
                and hasattr(self.generation_config, "no_timestamps_token_id")
                and init_tokens[i][-1] != self.generation_config.no_timestamps_token_id
            ):
                init_tokens[i].append(self.generation_config.no_timestamps_token_id)
            elif (
                self.generation_config.return_timestamps and init_tokens[i][-1] == self.generation_config.no_timestamps_token_id
            ):
                logger.info(
                    "<|notimestamps|> prompt token is removed from generation_config since `return_timestamps` is set to `'True'`."
                )
                init_tokens[i] = init_tokens[i][:-1]

            init_tokens[i] = [t for t in init_tokens[i] if t is not None]

        return np.tile(np.array(init_tokens, dtype=np.int64), (batch_size, 1))