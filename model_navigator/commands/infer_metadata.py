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
"""Inputs and outputs metadata commands."""
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from model_navigator.api.config import Sample, SizedDataLoader, SizedIterable, TensorRTProfile
from model_navigator.commands.base import Command, CommandOutput, CommandStatus
from model_navigator.exceptions import ModelNavigatorUserInputError
from model_navigator.execution_context import ExecutionContext
from model_navigator.logger import LOGGER
from model_navigator.utils.dataloader import extract_sample, sample_to_tuple, validate_sample_input
from model_navigator.utils.devices import is_cuda_available
from model_navigator.utils.format_helpers import FRAMEWORK2BASE_FORMAT
from model_navigator.utils.framework import Framework
from model_navigator.utils.runners import get_format_default_runners
from model_navigator.utils.tensor import TensorMetadata, TensorSpec


def _extract_axes_shapes(
    dataloader: Union[SizedDataLoader, Iterator],
    input_names: Sequence[str],
    input_ndims: Sequence[int],
    num_samples: int,
    framework: Framework,
    check_len: bool = True,
) -> Dict[str, Dict[int, List[int]]]:
    assert not (check_len) or isinstance(
        dataloader, (SizedIterable, Sequence)
    ), "dataloader is not an instance of SizedDataLoader, unable to check length."

    axes_shapes = {name: {ax: [] for ax in range(ndim)} for name, ndim in zip(input_names, input_ndims)}
    for i, sample in enumerate(dataloader):
        if i >= num_samples:
            LOGGER.warning(f"{len(dataloader)=}, but more samples found.")
            break
        validate_sample_input(sample, framework)
        sample = extract_sample(sample, input_names, framework)
        for name, tensor in sample.items():
            for k, dim in enumerate(tensor.shape):
                axes_shapes[name][k].append(dim)

    if check_len:
        assert i + 1 >= len(dataloader), f"{len(dataloader)=}, but only {i + 1} samples found."

    return axes_shapes


def _extract_max_batch_size(axes_shapes: Dict[str, Dict[int, List[int]]], batch_dim: Optional[int]) -> int:
    if batch_dim is not None:
        return max(list(axes_shapes.values())[0][batch_dim])
    return 0


def _get_trt_profile_from_axes_shapes(axes_shapes, batch_dim):
    trt_profile = TensorRTProfile()
    for name, axes in axes_shapes.items():
        min_max_opt = []
        for ax, shapes in axes.items():
            if ax == batch_dim:  # min bs = 1
                min_max_opt.append((1, int(np.median(shapes)), max(shapes)))
            else:
                min_max_opt.append((min(shapes), int(np.median(shapes)), max(shapes)))
        if min_max_opt:
            trt_profile.add(name, *list(zip(*min_max_opt)))
        else:
            raise ModelNavigatorUserInputError(
                f"Missing shape information for {name} input from dataloader."
                "Scalar values are not supported by Triton Inference Server."
                "Wrap it in tuple to add dimension e.g. tensor(3) -> tensor((3,))"
            )
    return trt_profile


def _get_metadata_from_axes_shapes(axes_shapes, batch_dim, dtypes):
    metadata = TensorMetadata()
    for name, axes in axes_shapes.items():
        tensor_shape = []
        for ax, shapes in axes.items():
            if ax == batch_dim or min(shapes) != max(shapes):
                tensor_shape.append(-1)
            else:
                tensor_shape.append(shapes[0])
        metadata.add(name, tuple(tensor_shape), dtypes[name])
    return metadata


def _update_user_dynamic_axes_inplace(dynamic_axes, input_metadata):
    for name, axes in dynamic_axes.items():
        axes = list(axes)
        tensor_spec = input_metadata.get(name, None)
        if tensor_spec is None:
            return
        shape = list(tensor_spec.shape)
        for ax in axes:
            shape[ax] = -1  # update
        input_metadata[name] = TensorSpec(name, tuple(shape), tensor_spec.dtype)
        for ax, d in enumerate(tensor_spec.shape):
            if d == -1 and ax not in axes:  # verify
                raise ValueError(
                    f"In tensor `{name}` axis `{ax}` is not set as dynamic axes but is dynamic in the dataloader."
                )


class InferInputMetadata(Command, is_required=True):
    """Command to collect model inputs metadata."""

    def _run(
        self,
        model: Union[object, Path],
        framework: Framework,
        dataloader: SizedDataLoader,
        _input_names: Optional[Tuple[str, ...]] = None,
        batch_dim: Optional[int] = None,
        dynamic_axes: Optional[Dict[str, Union[Dict[int, str], List[int]]]] = None,
    ) -> CommandOutput:
        """Execute the InferInputMetadata command.

        Args:
            framework: Framework of model to run inference
            model: A model object or path to file
            dataloader: Dataloader for providing samples
            _input_names: Name of model inputs
            dynamic_axes: Definition of model inputs dynamic axes
            batch_dim: Location of batch dimension in data samples

        Returns:
            CommandOutput object
        """
        sample = next(iter(dataloader))
        validate_sample_input(sample, framework)
        input_names = _input_names
        if input_names is None:
            input_names = self._get_default_input_names(model, sample, framework)

        input_sample = extract_sample(sample, input_names, framework)
        input_ndims = [t.ndim for t in input_sample.values()]
        input_dtypes = {n: t.dtype for n, t in input_sample.items()}
        num_samples = len(dataloader)
        axes_shapes = _extract_axes_shapes(dataloader, input_names, input_ndims, num_samples, framework)
        dataloader_max_batch_size = _extract_max_batch_size(axes_shapes, batch_dim)
        dataloader_trt_profile = _get_trt_profile_from_axes_shapes(axes_shapes, batch_dim)

        input_metadata = _get_metadata_from_axes_shapes(axes_shapes, batch_dim, input_dtypes)
        if dynamic_axes is None:
            LOGGER.warning(f"No dynamic axes provided. Using values derived from the dataloader: {input_metadata}")
        else:
            _update_user_dynamic_axes_inplace(dynamic_axes, input_metadata)

        return CommandOutput(
            status=CommandStatus.OK,
            output={
                "input_metadata": input_metadata,
                "dataloader_trt_profile": dataloader_trt_profile,
                "dataloader_max_batch_size": dataloader_max_batch_size,
            },
        )

    @staticmethod
    def _get_default_input_names(
        model,
        sample,
        framework,
    ):
        input_tuple = sample_to_tuple(sample)
        if framework == Framework.ONNX:
            from model_navigator.runners.onnx import OnnxrtCPURunner, OnnxrtCUDARunner

            assert isinstance(model, Path), "ONNX model must be a pathlib.Path"

            onnxrt_runner_cls = OnnxrtCUDARunner if is_cuda_available() else OnnxrtCPURunner
            onnx_runner = onnxrt_runner_cls(
                model=model,
                input_metadata=TensorMetadata(),
                output_metadata=TensorMetadata(),
                disable_fallback=False,
            )
            with onnx_runner:
                input_metadata = onnx_runner.get_onnx_input_metadata()
                input_names = tuple(input_metadata.keys())
        elif isinstance(sample, Mapping):
            input_names = tuple(sample.keys())
        else:
            input_names = tuple(f"input__{i}" for i in range(len(input_tuple)))
        return input_names


class InferOutputMetadata(Command, is_required=True):
    """Command to collect model outputs  metadata."""

    def _run(
        self,
        framework: Framework,
        model: Union[object, Path],
        dataloader: SizedDataLoader,
        profiling_sample: Sample,
        conversion_samples: List[Sample],
        input_metadata: TensorMetadata,
        workspace: Path,
        verbose: bool,
        _output_names: Optional[Tuple[str, ...]] = None,
        dynamic_axes: Optional[Dict[str, Union[Dict[int, str], List[int]]]] = None,
        forward_kw_names: Optional[Tuple[str, ...]] = None,
        batch_dim: Optional[int] = None,
    ) -> CommandOutput:
        """Execute the InferOutputMetadata command.

        Args:
            framework: Framework of model to run inference
            model: A model object or path to file
            dataloader: Dataloader for providing samples
            profiling_sample: Profiling sample
            conversion_samples: Conversion samples
            input_metadata: Model inputs metadata
            workspace: Working directory where command should be executed
            verbose: Enable verbose logging
            _output_names: Name of model outputs
            dynamic_axes: Definition of model outputs dynamic axes
            forward_kw_names: Forward keyword arguments (used for TensorFlow model)
            batch_dim: Location of batch dimension in data samples

        Returns:
            CommandOutput object
        """
        if _output_names:
            temp_output_metadata = TensorMetadata({out_name: TensorSpec(out_name, ()) for out_name in _output_names})
        else:
            temp_output_metadata = TensorMetadata()

        runner = get_format_default_runners(FRAMEWORK2BASE_FORMAT[framework])[0](
            model=model,
            input_metadata=input_metadata,
            output_metadata=temp_output_metadata,
            input_metadata_mapping=forward_kw_names,
            disable_fallback=False,
        )  # pytype: disable=not-instantiable

        with runner, ExecutionContext(workspace=workspace, verbose=verbose):
            output_sample = runner.infer(profiling_sample)
            output_names = list(output_sample.keys())
            output_generator = (runner.infer(sample) for sample in conversion_samples)

            output_ndims = [t.ndim for t in output_sample.values()]
            output_dtypes = {n: t.dtype for n, t in output_sample.items()}
            num_samples = len(dataloader)
            axes_shapes = _extract_axes_shapes(
                output_generator, output_names, output_ndims, num_samples, framework, check_len=False
            )

        output_metadata = _get_metadata_from_axes_shapes(axes_shapes, batch_dim, output_dtypes)
        if dynamic_axes is None:
            LOGGER.warning(f"No dynamic axes provided. Using values derived from the dataloader: {output_metadata}")
        else:
            _update_user_dynamic_axes_inplace(dynamic_axes, output_metadata)

        return CommandOutput(
            status=CommandStatus.OK,
            output={"output_metadata": output_metadata},
        )