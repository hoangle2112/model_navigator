# Copyright (c) 2021-2022, NVIDIA CORPORATION. All rights reserved.
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

from dataclasses import dataclass
from typing import List

from model_navigator.framework_api.commands.core import Command, CommandResults
from model_navigator.framework_api.config import Config
from model_navigator.framework_api.logger import LOGGER
from model_navigator.framework_api.utils import DataObject, Framework, pad_string


@dataclass
class PipelineResults(DataObject):
    name: str
    id: str
    framework: Framework
    commands_results: List[CommandResults]


class Pipeline:
    def __init__(
        self,
        name: str,
        framework: Framework,
        commands: List[Command],
    ):
        self.name = name
        self.id = name.lower().replace(" ", "_").replace("-", "_")
        self.framework = framework
        self._commands = commands

    def __call__(self, config: Config, **kwargs):
        LOGGER.info(pad_string(f"Pipeline {self.name} started"))
        additional_params = {}

        commands_results = []
        for command in self._commands:
            LOGGER.info(pad_string(f"Command {command.name} started"))
            results = command.transform(**{**config.to_dict(), **additional_params})
            if command.get_output_name() is not None:
                additional_params[command.get_output_name()] = results.output

            commands_results.append(results)

        pipeline_results = PipelineResults(
            self.name,
            self.id,
            self.framework,
            commands_results,
        )

        return pipeline_results
