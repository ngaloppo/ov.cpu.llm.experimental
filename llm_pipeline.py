import argparse
import json
import time
import hashlib
import numpy as np
import sys
import csv
from pathlib import Path
from openvino.runtime import Core, Model, Tensor, PartialShape, Type, serialize, opset_utils
from openvino.runtime import opset10 as opset
from openvino.preprocess import PrePostProcessor
from transformers import AutoTokenizer, AutoModelForCausalLM
from pipeline.greedy_search import generate_greedy
from pipeline.beam_search import generate_beam
from models.utils import OV_XML_FILE_NAME

class ModelConfig:
    def __init__(self, ov_model) -> None:
        kv_cache_shape = ov_model.input("kv_cache").partial_shape
        cos_tab_shape = ov_model.input("cos_tab").partial_shape

        # 2*n_layers, B, H, L, S
        self.n_layers = kv_cache_shape[0].get_length() // 2
        self.n_head = kv_cache_shape[2].get_length()
        self.head_size = kv_cache_shape[4].get_length()
        self.rotary_dims = cos_tab_shape[1].get_length() * 2 # assumes sin/cos table dims is half of rotary_dims

    def __str__(self) -> str:
        return f"\tn_layers={self.n_layers}, n_head={self.n_head}, head_size={self.head_size}, rotary_dims={self.rotary_dims}"

def post_processing(result, input_text): 
    """post processing the model output"""
    ans = result
    if result[:len(input_text)] == input_text:
        ans = result[len(input_text):]
    return ans

last_output_text_map = {}

def generate(args, text, tokenizer, compiled_model, enforce_input_tokens = None):
    global last_output_text_map

    if enforce_input_tokens:
        inputs = tokenizer(text, return_tensors="np", padding=True, return_token_type_ids=False)
        input_ids = inputs['input_ids']
        attention_mask = inputs['attention_mask']
        attention_mask = (1.0 - attention_mask) * np.finfo(np.float32).min

        input_ids = np.tile(input_ids[:, 0:1], enforce_input_tokens)
        attention_mask = np.tile(attention_mask[:, 0:1], enforce_input_tokens)

        input_token_len = input_ids.shape[1]
        input_batch_size = input_ids.shape[0]
    else:
        inputs = tokenizer(text, return_tensors="np", padding=True, return_token_type_ids=False)
        input_ids = inputs['input_ids']
        attention_mask = inputs['attention_mask']
        attention_mask = (1.0 - attention_mask) * np.finfo(np.float32).min

        input_token_len = input_ids.shape[1]
        input_batch_size = input_ids.shape[0]

    gen_sequence_start = time.time()
    if args.greedy:
        output_ids, latency = generate_greedy(compiled_model, input_ids, attention_mask, 
                                    max_new_tokens=args.answer_length,
                                    eos_token_id=tokenizer.eos_token_id,
                                    pad_token_id=tokenizer.pad_token_id,
                                    max_kv_len=input_token_len + args.answer_length*2)
    else:
        output_ids, latency = generate_beam(compiled_model, input_ids, attention_mask, 
                                    max_new_tokens=args.answer_length,
                                    eos_token_id=tokenizer.eos_token_id,
                                    pad_token_id=tokenizer.pad_token_id,
                                    max_kv_len=input_token_len + args.answer_length*2,
                                    beam_size=args.beam_size)
    gen_sequence_end = time.time()
    output_text = tokenizer.batch_decode(output_ids, skip_special_tokens=True)

    gen_sequence_length = len(output_ids[0]) - len(input_ids[0])
    gen_latency = gen_sequence_end - gen_sequence_start

    n_latency = len(latency)
    token_total = sum(latency)

    average_token_latency = sum(latency[2:])/(n_latency-2)
    overhead_latency = gen_latency - token_total
    
    print(f"  [{input_batch_size}, {input_token_len:4}+{gen_sequence_length}]  {gen_latency*1e3:.1f}ms = {latency[0]*1e3:.1f}ms + {latency[1]*1e3:.1f}ms + ({average_token_latency*1e3:.1f}ms x {n_latency-2}) + {overhead_latency * 1e3:.1f}ms")

    text_key = ",".join(text)

    if text_key not in last_output_text_map or last_output_text_map[text_key] != output_text:
        last_output_text_map[text_key] = output_text
        for i, out in enumerate(output_text):
            md5sum = hashlib.md5(out.encode('utf-8')).hexdigest()
            console_out = post_processing(out, text)
            if len(console_out) > 160:
                console_out = console_out[:80] + "..." + md5sum
            print(f"\t{i}. {[console_out]}")

    benchmark_data = {
        'input_batch_size': input_batch_size,
        'input_token_length': input_token_len,
        'generated_sequence_length': gen_sequence_length,
        'generation_latency_total_ms': gen_latency * 1e3,
        'token_latency_first_ms': latency[0] * 1e3,
        'average_token_latency_ms': average_token_latency * 1e3,
        'overhead_ms': overhead_latency * 1e3,
        'output': post_processing(output_text[0], text)
    }

    return benchmark_data


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Add an argument
    parser.add_argument('-m', '--model', type=str, required=True,
                        help="path to model directory, which contains OpenVINO model and tokenzier")
    parser.add_argument('-pl', '--prompt-length', type=int, nargs='+', default=32, required=False,
                        help="prompt length")
    parser.add_argument('-p', '--prompt', type=str, nargs='+', required=False,
                        help="prompt")
    parser.add_argument('-al', '--answer-length', type=int,
                        default=32, help="generated token length")
    parser.add_argument("--greedy", action="store_true")
    parser.add_argument("--bf16", action="store_true")
    parser.add_argument("-bs", "--beam-size", type=int, default=4)
    parser.add_argument("-r", "--repeat", type=int, default=1)
    parser.add_argument("--prompts", type=str, help="Process prompts from given file")
    parser.add_argument("--output-results", type=str, help="Output results to JSONLines file")
    # Parse the argument
    args = parser.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.add_special_tokens({'pad_token': '[PAD]'})
        tokenizer.pad_token = tokenizer.eos_token_id
    tokenizer.padding_side = "left"             # pad to left

    ext_path = None
    if sys.platform == 'win32':
        ext_path = ".\\custom_ops\\build\\Release\\ov-cpu-llm-experimental.dll"
    elif sys.platform == 'linux':
        ext_path = "./custom_ops/build/libov-cpu-llm-experimental.so"
    else:
        print(f"Sample code not supported on platform: {sys.platform}")
        exit(1)

    # initialize openvino core
    core = Core()
    custom_opset = opset_utils._get_node_factory()
    custom_opset.add_extension(ext_path)
    core.add_extension(ext_path)
    print("Init OpenVINO model ...")
    # read the model and corresponding weights from file
    ov_model = core.read_model(Path(args.model) / OV_XML_FILE_NAME)

    # add preprocessor for bf16 kv_cache
    if args.bf16:
        kv_cache_precision = Type.bf16
        ppp = PrePostProcessor(ov_model)
        for key in ov_model.inputs:
            if "kv_cache" in key.get_any_name() and kv_cache_precision != key.get_element_type():
                ppp.input(key.get_any_name()).tensor().set_element_type(kv_cache_precision)
        ov_model = ppp.build()

    ov_config={"PERFORMANCE_HINT": "LATENCY", "NUM_STREAMS": 1,
                "INFERENCE_PRECISION_HINT" : "bf16" if args.bf16 else "f32",
                "CPU_DENORMALS_OPTIMIZATION" : "YES",
                "CACHE_DIR" : None}

    compiled_model = core.compile_model(ov_model, "CPU", ov_config)
    compiled_model.pipeline_config = ModelConfig(ov_model)

    prompts_fn = "prompts.json"
    if args.prompts:
        prompts_fn = args.prompts
    with open(prompts_fn) as f:
        prompts = json.load(f)

    enforce_input_tokens = False
    if args.prompt:
        # prompt from command line
        prompts = [args.prompt]
    elif args.prompts:
        # load all prompts from a prompts file
        prompts = prompts.values()
    else:
        # prompt, with prescribed prompt from json config
        prompts = []
        for plen in args.prompt_length:
            if str(plen) in prompts:
                prompts.append(prompts[str(plen)])
            else:
                prompts = ["Hi"]
                # Prompt with length {plen} is not provided in prompt.json, will forge"
                enforce_input_tokens = True

    print("Start test ...")
    benchmark_data = []
    for prompt in prompts:
        for round in range(args.repeat):
            print(f"round {round}:")
            result = generate(args, prompt, tokenizer, compiled_model, enforce_input_tokens=enforce_input_tokens)
            benchmark_data.append(result)

    if args.output_results:
        with open(args.output_results, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=benchmark_data[0].keys())
            writer.writeheader()
            for data in benchmark_data:
                writer.writerow(data)
