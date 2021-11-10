# Copyright (c) 2021, NVIDIA CORPORATION. All rights reserved.
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

from typing import Dict, List


def equal_model_configs_sets(a: List[Dict], b: List[Dict]):
    a = a.copy()
    b = b.copy()

    same = None
    for item in a:
        if item not in b:
            same = False
            break
        b.remove(item)
    if same is None:
        same = not b
    else:
        same = False
    return same