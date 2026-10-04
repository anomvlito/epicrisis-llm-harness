"""vLLM engine for form199 v2.0.

Same chat templates and user framing as ../extraction/run_experiment.py (the Transformers engine of
versions 1.x): the prompt is rendered with the model's own tokenizer and passed to vLLM as token IDs.
Generation stops at the closing code fence or end of sequence, and the text after the first complete
JSON object is cut, which reproduces the stopping criterion of version 1.x.
"""
from __future__ import annotations
import os
import sysconfig
import time

MODELS = {
    'Gemma4': '/path/to/models/google/gemma-4-31B-it',
    'Llama-70B': '/path/to/models/meta-llama/Llama-3.3-70B-Instruct',
    'Qwen': '/path/to/models/Qwen/Qwen3.6-35B-A3B',
}
FAMILY = {'Gemma4': 'gemma', 'Llama-70B': 'llama', 'Qwen': 'qwen'}
USER_PREFIX = 'Analiza la siguiente nota clínica y devuelve el JSON estructurado.\n\nNOTA CLÍNICA COMPLETA:\n'
USER_SUFFIX = '\n\nResponde SOLO con el JSON válido.'
# RTX A6000 (sm86) has no FP8 tensor cores: disable W8A8 kernels so FP8 weights run on Marlin (16-bit math).
NON_MARLIN_FP8_KERNELS = ['FlashInferFP8ScaledMMLinearKernel', 'CutlassFP8ScaledMMLinearKernel',
                          'B12xTensorFP8ScaledMMLinearKernel', 'PerTensorTorchFP8ScaledMMLinearKernel',
                          'ChannelWiseTorchFP8ScaledMMLinearKernel']


def find_complete_json_end(text):
    """Index after the first complete top-level JSON object (same rule as run_experiment.py)."""
    start = text.find('{')
    if start < 0:
        return None
    depth, in_string, escaped = 0, False, False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def build_messages(system_prompt, note, family):
    user = USER_PREFIX + note + USER_SUFFIX
    if family == 'gemma':
        return [{'role': 'user', 'content': system_prompt + '\n\n' + user}]
    return [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user}]


def configure_environment(engine_settings):
    os.environ.setdefault('VLLM_DISABLED_KERNELS', ','.join(NON_MARLIN_FP8_KERNELS))
    os.environ.setdefault('VLLM_USE_FLASHINFER_SAMPLER', '0')  # greedy decoding needs no sampler kernel
    os.environ.setdefault('HF_HUB_OFFLINE', '1')
    cuda_home = os.path.join(sysconfig.get_paths()['purelib'], 'nvidia', 'cu13')
    if os.path.isdir(cuda_home):
        os.environ.setdefault('CUDA_HOME', cuda_home)
        os.environ['PATH'] = os.path.join(cuda_home, 'bin') + os.pathsep + os.environ.get('PATH', '')
    os.environ['VLLM_BATCH_INVARIANT'] = '1' if engine_settings.get('batch_invariant') else '0'


def split_output(text, finish_reason):
    """Cut after the first complete JSON object; describe how generation ended."""
    end = find_complete_json_end(text)
    raw = text[:end] if end is not None else text
    reason = 'json_complete' if end is not None else ('max_tokens' if finish_reason == 'length' else 'eos')
    return raw, {'finish_reason': reason, 'truncated': finish_reason == 'length' and end is None,
                 'json_complete': end is not None,
                 'characters_after_json': len(text) - end if end is not None else None}


class Engine:
    def __init__(self, model_name, engine_settings):
        configure_environment(engine_settings)
        from transformers import AutoTokenizer
        from vllm import LLM
        import vllm
        self.model_name, self.family = model_name, FAMILY[model_name]
        path = MODELS[model_name]
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
        kwargs = dict(model=path, tensor_parallel_size=engine_settings['tensor_parallel_size'],
                      quantization=engine_settings['quantization'], max_model_len=engine_settings['max_model_len'],
                      gpu_memory_utilization=engine_settings['gpu_memory_utilization'],
                      enforce_eager=engine_settings['enforce_eager'],
                      enable_prefix_caching=engine_settings['enable_prefix_caching'],
                      kv_cache_dtype=engine_settings.get('kv_cache_dtype', 'auto'), seed=0)
        if engine_settings.get('pipeline_parallel_size', 1) > 1:
            kwargs['pipeline_parallel_size'] = engine_settings['pipeline_parallel_size']
        if self.family in ('gemma', 'qwen'):
            kwargs['limit_mm_per_prompt'] = {'image': 0}
        self.llm = LLM(**kwargs)
        self.setup = {'engine': 'vllm', 'vllm_version': vllm.__version__, **engine_settings,
                      'batch_invariant_env': os.environ['VLLM_BATCH_INVARIANT']}

    def encode(self, system_prompt, note):
        kwargs = {'tokenize': False, 'add_generation_prompt': True}
        if self.family in ('gemma', 'qwen'):
            kwargs['enable_thinking'] = False
        text = self.tokenizer.apply_chat_template(build_messages(system_prompt, note, self.family), **kwargs)
        return self.tokenizer(text, add_special_tokens=False)['input_ids']

    def generate(self, requests):
        """requests: list of (system_prompt, note, max_new_tokens). Returns [(raw_text, meta)] in order."""
        from vllm import SamplingParams
        from vllm.inputs import TokensPrompt
        prompts = [TokensPrompt(prompt_token_ids=self.encode(p, n)) for p, n, _ in requests]
        params = [SamplingParams(temperature=0.0, max_tokens=b, stop=['\n```'], seed=0) for _, _, b in requests]
        started = time.time()
        outputs = self.llm.generate(prompts, params, use_tqdm=False)
        wall = time.time() - started
        results = []
        for out in outputs:
            gen = out.outputs[0]
            raw, info = split_output(gen.text, gen.finish_reason)
            results.append((raw, {**info, 'raw_response': raw, 'raw_response_full': gen.text,
                                  'tokens_entrada': len(out.prompt_token_ids), 'tokens_salida': len(gen.token_ids),
                                  'batch_size': len(requests), 'batch_wall_s': round(wall, 3),
                                  # share of the batch wall time, so that summing over calls gives GPU time
                                  'latencia_s': round(wall / len(requests), 3), 'engine': self.setup}))
        return results
