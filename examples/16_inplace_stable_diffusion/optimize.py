#!/usr/bin/env python3
# Copyright (c) 2021-2023, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import time

# pytype: disable=import-error
import torch
from diffusers import DPMSolverMultistepScheduler, StableDiffusionPipeline
from diffusers.models.unet_2d_condition import UNet2DConditionOutput
from diffusers.models.vae import DecoderOutput
from transformers.modeling_outputs import BaseModelOutputWithPooling

import model_navigator as nav

# pytype: enable=import-error


nav.inplace_config.mode = os.environ.get("MODEL_NAVIGATOR_INPLACE_MODE", nav.inplace_config.mode)
nav.inplace_config.min_num_samples = int(
    os.environ.get("MODEL_NAVIGATOR_MIN_NUM_SAMPLES", nav.inplace_config.min_num_samples)
)


LOGGER = logging.getLogger("model_navigator.inplace")
logging.basicConfig(level=logging.INFO)


def get_pipeline():
    model_id = "stabilityai/stable-diffusion-2-1"
    pipe = StableDiffusionPipeline.from_pretrained(model_id, torch_dtype=torch.float16)
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config)
    pipe = pipe.to("cuda")

    optimize_config = nav.OptimizeConfig(
        batching=False,
        target_formats=(nav.Format.TENSORRT,),
        runners=(
            "TorchCUDA",
            "TensorRT",
        ),
    )

    # Currently Model Navigator does not support inplace optimization of models with non-tensor inputs.
    # We can work around this by removing the non-tensor inputs from the input mapping, since they are not used
    # in the model. In the future, we will add support for non-tensor inputs.
    def clip_input_mapping(input):
        del input["attention_mask"]
        return input

    def unet_input_mapping(input):
        del input["return_dict"]
        del input["cross_attention_kwargs"]
        input["timestep"] = input["timestep"].unsqueeze(0)
        return input

    # For outputs that are not primitive types (float, int, bool, str) or tensors and list, dict, tuples combinations of those.
    # we need to provide a mapping to a desired output type. By default Model Navigator will return a flatten dict of tensors.
    # In this case, we need to map the outputs of the models to custom HuggingFace classes.
    def clip_output_mapping(output):
        return BaseModelOutputWithPooling(*list(output.values()))

    def unet_output_mapping(output):
        return UNet2DConditionOutput(*list(output.values()))

    def vae_output_mapping(output):
        return DecoderOutput(*list(output.values()))

    pipe.text_encoder = nav.Module(
        pipe.text_encoder,
        optimize_config=optimize_config,
        input_mapping=clip_input_mapping,
        output_mapping=clip_output_mapping,
    )
    pipe.unet = nav.Module(
        pipe.unet,
        optimize_config=optimize_config,
        input_mapping=unet_input_mapping,
        output_mapping=unet_output_mapping,
    )
    pipe.vae.decoder = nav.Module(
        pipe.vae.decoder,
        optimize_config=optimize_config,
        output_mapping=vae_output_mapping,
    )

    return pipe


def get_dataloader():
    return ["a photo of an astronaut riding a horse on mars"]


def main():
    pipe = get_pipeline()
    dataloader = get_dataloader()

    start = time.monotonic()
    image = pipe(dataloader[0]).images[0]
    end = time.monotonic()
    LOGGER.info(f"Elapsed time: {end - start:.2f} seconds")

    image.save(f"astronaut_rides_horse_{nav.inplace_config.mode.value}.png")  # pytype: disable=attribute-error


if __name__ == "__main__":
    main()