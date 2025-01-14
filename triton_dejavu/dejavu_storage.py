#  /*******************************************************************************
#   * Copyright 2024 IBM Corporation
#   *
#   * Licensed under the Apache License, Version 2.0 (the "License");
#   * you may not use this file except in compliance with the License.
#   * You may obtain a copy of the License at
#   *
#   *     http://www.apache.org/licenses/LICENSE-2.0
#   *
#   * Unless required by applicable law or agreed to in writing, software
#   * distributed under the License is distributed on an "AS IS" BASIS,
#   * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   * See the License for the specific language governing permissions and
#   * limitations under the License.
#  *******************************************************************************/
#

import sys
import os
import json
import torch
import triton
import hashlib
import time
import asyncio
from distutils.util import strtobool

from triton_dejavu import __version__ as dejavu_version
from .dejavu_utilities import (
    get_storage_identifier,
    flag_print_debug,
    flag_print_debug_verbose,
)


__storage_env_var__ = "TRITON_DEJAVU_STORAGE"
__tag_env_var__ = "TRITON_DEJAVU_TAG"
__tag_default__ = "default"
storage_tag = os.environ.get(__tag_env_var__, __tag_default__)


def _create_tuple(k):
    s = k[1:-1]
    entries = s.split(", ")
    ret = []
    for e in entries:
        if e[0] == "'" or e[0] == '"':
            ret.append(e[1:-1])
        else:
            ret.append(e)
    ret_t = tuple(ret)
    return ret_t


__int_config_args__ = ["num_warps", "num_stages", "num_ctas"]
__int_or_none_config_args__ = ["maxnreg"]
__bool_config_args__ = ["enable_warp_specialization"]
__config_args__ = (
    ["pre_hook"]
    + __int_config_args__
    + __bool_config_args__
    + __int_or_none_config_args__
)
__skip_config_args__ = ["enable_persistent"]


def _get_cache_template(fn, configs_len=0):
    ret = {
        "signature": str(fn),
        "total_bench_time_s": 0.0,
        "evaluated_configs": configs_len,
        "cache": {},
        "timings": {},
    }
    return ret


def _create_config_args(v):
    vlist = v.split(", ")
    ret = {"kwargs": {}}
    for e in vlist:
        sl = e.split(": ")
        if sl[0] in __skip_config_args__:
            continue
        if sl[0] in __config_args__:
            if sl[0] in __int_config_args__:
                ret[sl[0]] = int(sl[1])
            elif sl[0] in __bool_config_args__:
                ret[sl[0]] = bool(strtobool(sl[1]))
            elif sl[0] in __int_or_none_config_args__:
                try:
                    ret[sl[0]] = int(sl[1])
                except ValueError:
                    ret[sl[0]] = None
            else:
                ret[sl[0]] = sl[1]
        else:
            try:
                ret["kwargs"][sl[0]] = int(sl[1])
            except ValueError:
                try:
                    ret["kwargs"][sl[0]] = bool(strtobool(sl[1]))
                except ValueError:
                    print(
                        f"[triton-dejavu] WARNING: can't determine type of kwarg {sl[0]}: {sl[1]}"
                    )
                    ret["kwargs"][sl[0]] = sl[1]
    return ret


async def _get_fn_hash(fn: triton.JITFunction):
    # trigger JIT
    test = fn.cache_key
    while fn.hash is None:
        await asyncio.sleep(0.1)
    fn_hash = fn.hash
    return fn_hash


def _wait_fn_hash(fn):
    loop = asyncio.new_event_loop()
    task = loop.create_task(_get_fn_hash(fn))
    loop.run_until_complete(task)
    fn_hash = task.result()
    return fn_hash


def _get_folder_name(fn_name, fn_hash, configs_hash, key_hash, param_hash):
    # return f"{fn_name}-{fn_hash}-{configs_hash}-{key_hash}-{param_hash}/{storage_tag}"
    # hash_of_hash = get_string_hash(f"{fn_hash}-{configs_hash}-{key_hash}-{param_hash}")
    # folder_tree_name = f"{fn_name}-{hash_of_hash}/{storage_tag}"
    hash_of_hash = get_string_hash(f"{fn_hash}-{key_hash}")
    folder_tree_name = f"{fn_name}/autotune_config-{param_hash}/kernel_configs-{configs_hash}/code_version-{hash_of_hash}/{storage_tag}"
    return folder_tree_name


def get_config_list_hash(configs):
    # sorted_configs = configs.sort()
    # order of config list should be the same if come from ConfigSpace
    # if manual, then can't be guaranteed
    #  (but may not matter in practice, since then also the source code may has changed?)
    s = "|"
    for c in configs:
        s += f"{c}|"
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    # triton jit uses sha1?
    # h = hashlib.sha1(s.encode('utf-8')).hexdigest()
    return h


def get_list_hash(l):
    s = "|".join(l)
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    # triton jit uses sha1?
    # h = hashlib.sha1(s.encode('utf-8')).hexdigest()
    return h


def get_string_hash(s):
    h = hashlib.sha256(s.encode("utf-8")).hexdigest()
    # triton jit uses sha1?
    # h = hashlib.sha1(s.encode('utf-8')).hexdigest()
    return h


class DejavuStorage:
    def __init__(self) -> None:
        self.storage_prefix = os.environ.get(__storage_env_var__, "none")
        if self.storage_prefix == "none":
            raise Exception(
                f"[triton-dejavu] The environment variable {__storage_env_var__} must be set for triton-dejavu!"
            )
        self.storage_identifier = get_storage_identifier()
        self.storage_path = os.path.abspath(
            f"{self.storage_prefix}/{self.storage_identifier}/"
        )
        if not os.path.exists(self.storage_path):
            # 0777 permissions to avoid problems with different users in containers and host system
            os.makedirs(self.storage_path, 0o0777)
        self.fn_storage = {}
        self.measured_timings = {}
        self._known_files = []
        self.used_configs = {}

    def __store__(self):
        for folder_name in self.fn_storage:
            dir_name = f"{self.storage_path}/{folder_name}/"
            if not os.path.exists(dir_name):
                os.makedirs(dir_name, 0o0777)
            file_name = f"{dir_name}/cache.json"
            if file_name not in self._known_files:
                self._known_files.append(file_name)
            with open(file_name, "w") as f:
                json.dump(self.fn_storage[folder_name], f, indent=4)
            try:
                os.chmod(dir_name, 0o0777)
            except PermissionError as e:
                print(f"can't set permission of directory {dir_name}: {e}")

    def add_autotuner_cache(
        self,
        cache,
        fn,
        configs_hash,
        key_hash,
        param_hash,
        configs_len,
        timings,
        repetitiont,
        warmupt,
        bench_time,
    ):
        fn_hash = _wait_fn_hash(fn)
        fn_name = str(fn).split(":")[1][:-1]
        folder_name = _get_folder_name(
            fn_name, fn_hash, configs_hash, key_hash, param_hash
        )
        if folder_name not in self.fn_storage:
            cache_json = _get_cache_template(fn, configs_len)
            tmp_used_configs = []
        else:
            # TODO: reload content to avoid overwriting in case of parallel processes?
            cache_json = self.fn_storage[folder_name]
            tmp_used_configs = self.used_configs[folder_name]
        changes_made = False
        for key, config in cache.items():
            if str(key) in cache_json["cache"]:
                continue
            # compatability with cuda stream feature of triton 3
            vals = timings[key]
            if type(vals) is not list:
                vals = [vals]
            if len(vals) == 1:
                labels = ["ms"]
            else:
                labels = ["ms", "min_ms", "max_ms"]
            nt = {
                "values": vals,
                "labels": labels,
                "rep_t_ms": repetitiont,
                "warmup_t_ms": warmupt,
            }
            if float("inf") in nt["values"]:
                continue
            cache_json["cache"][str(key)] = str(config)
            cache_json["timings"][str(key)] = nt
            cache_json["evaluated_configs"] = configs_len
            if config not in tmp_used_configs:
                tmp_used_configs.append(config)
            changes_made = True
            if flag_print_debug:
                print(
                    f"[triton-dejavu] added {str(config)} for {folder_name} and key {key}"
                )
        if changes_made:
            cache_json["total_bench_time_s"] += bench_time
            self.fn_storage[folder_name] = cache_json
            self.used_configs[folder_name] = tmp_used_configs
            self.__store__()

    def restore_autotuner_cache(self, fn, configs_hash, key_hash, param_hash):
        # we need to consider dependencies as well, so we will wait for fn.hash
        fn_hash = _wait_fn_hash(fn)
        fn_name = str(fn).split(":")[1][:-1]
        folder_name = _get_folder_name(
            fn_name, fn_hash, configs_hash, key_hash, param_hash
        )
        cache_file = f"{self.storage_path}/{folder_name}/cache.json"
        if not os.path.isfile(cache_file):
            if flag_print_debug:
                print(f"[triton-dejavu] No configurations found for {folder_name}.")
            # create cache file early
            cache_json = _get_cache_template(fn)
            self.fn_storage[folder_name] = cache_json
            self.used_configs[folder_name] = []
            self.__store__()
            return {}
        if cache_file not in self._known_files:
            self._known_files.append(cache_file)
        with open(cache_file, "r") as f:
            cache_json = json.load(f)
        self.fn_storage[folder_name] = cache_json
        ret = {}
        tmp_used_configs = []
        for k, v in cache_json["cache"].items():
            kt = _create_tuple(k)
            va = _create_config_args(v)
            c = triton.Config(**va)
            ret[kt] = c
            if c not in tmp_used_configs:
                tmp_used_configs.append(c)
            if flag_print_debug_verbose:
                print(
                    f"[triton-dejavu] restored {str(c)} for {folder_name} and key {kt}"
                )
        self.used_configs[folder_name] = tmp_used_configs
        if flag_print_debug:
            print(
                f"[triton-dejavu] restored {len(ret)} configurations for {folder_name}."
            )
        return ret

    def get_used_configs(self, fn, configs_hash, key_hash, param_hash):
        fn_hash = _wait_fn_hash(fn)
        fn_name = str(fn).split(":")[1][:-1]
        folder_name = _get_folder_name(
            fn_name, fn_hash, configs_hash, key_hash, param_hash
        )
        return self.used_configs[folder_name]

    def dump_storage(self, filter_timings=False):
        print(
            f"DejavuStorage path:\t\t{self.storage_prefix}\nDejavuStorage identifier:\t{self.storage_identifier}"
        )
        if filter_timings:
            tmp_json = {}
            for k, v in self.fn_storage.items():
                tmp_d = {}
                for kk, vv in v.items():
                    if kk == "timings":
                        continue
                    tmp_d[kk] = vv
                tmp_json[k] = tmp_d
            print(json.dumps(tmp_json, indent=4))
        else:
            print(json.dumps(self.fn_storage, indent=4))


global_dejavu_storage = DejavuStorage()
