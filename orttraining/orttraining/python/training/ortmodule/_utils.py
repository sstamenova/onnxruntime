# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

from onnxruntime.capi.onnxruntime_inference_collection import OrtValue
from onnxruntime.capi import _pybind_state as C
from ._fallback_exceptions import ORTModuleDeviceException, wrap_exception
from ._torch_module_pytorch import TorchModulePytorch

import os
import copy
import inspect
import torch
from torch.utils.dlpack import from_dlpack, to_dlpack
import traceback
from typing import List
import types
import warnings
from distutils.version import LooseVersion

def _ortvalue_from_torch_tensor(torch_tensor):
    # TODO: Current DLPack doesn't support bool and PyTorch disables converting bool tensor to DLPack in recent commit.
    # https://github.com/pytorch/pytorch/blob/7e7be526c9d9179f35084e9cca5b5c5ad5172100/aten/src/ATen/DLConvertor.cpp#L41
    # We need to convert bool tensor to unit8 tensor to workaround this.
    # DLPack is discussing how to support bool type, we can remove this workaround once both DLPack
    # and PyTorch support bool type.
    is_bool_tensor = torch_tensor.dtype == torch.bool
    if is_bool_tensor and LooseVersion(torch.__version__) >= LooseVersion('1.10.0'):
        torch_tensor = torch_tensor.to(torch.uint8)
    return C.OrtValue.from_dlpack(to_dlpack(torch_tensor), is_bool_tensor)


def _torch_tensor_from_dl_pack(dlpack, ortvalue, device):
    torch_tensor = from_dlpack(dlpack) if device.type != 'ort' else C.ort_from_dlpack(dlpack)
    return torch_tensor.to(torch.bool) if ortvalue.data_type() == 'tensor(bool)' else torch_tensor


def _ortvalue_to_torch_tensor(ortvalue, device):
    # PyTorch's to_dlpack() uses same config for both torch.bool and torch.uint8,
    # and convert the config to torch.uint8 tensor duing from_dlpack().
    # So we need to convert the torch tensor to torch.bool type if OrtValue is bool tensor.
    dlpack_tensor = ortvalue.to_dlpack()
    return _torch_tensor_from_dl_pack(dlpack_tensor, ortvalue, device)

def _torch_tensor_to_dlpack(tensor):
    if tensor.device.type == 'ort':
        return C.ort_to_dlpack(tensor)
    else:
        # TODO: Current DLPack doesn't support bool and PyTorch disables converting bool tensor to DLPack in recent commit.
        # https://github.com/pytorch/pytorch/blob/7e7be526c9d9179f35084e9cca5b5c5ad5172100/aten/src/ATen/DLConvertor.cpp#L41
        # We need to convert bool tensor to unit8 tensor to workaround this.
        # DLPack is discussing how to support bool type, we can remove this workaround once both DLPack
        # and PyTorch support bool type.
        if tensor.dtype == torch.bool and LooseVersion(torch.__version__) >= LooseVersion('1.10.0'):
            tensor = tensor.to(torch.uint8)
        return to_dlpack(tensor)


def _check_same_device(device, argument_str, *args):
    '''Check that all tensor arguments in *args reside on the same device as the input device'''

    assert isinstance(device, torch.device), '`device` must be a valid `torch.device` object'
    for arg in args:
        if arg is not None and isinstance(arg, torch.Tensor):
            arg_device = torch.device(arg.device)
            if arg_device != device:
                raise wrap_exception(ORTModuleDeviceException,
                                     RuntimeError(
                                         f"{argument_str} found on device {arg_device}, but expected it to be on module device {device}."))


def get_device_index(device):
    if isinstance(device, str):
        # could be 'cuda:0', 'cuda:1', or 'cpu'. with cpu, set index=0
        device = torch.device(device)
    elif isinstance(device, int):
        return device
    return 0 if device.index is None else device.index


def get_device_str(device):
    if isinstance(device, str):
        # could be 'cuda:0', 'cuda:1', or 'cpu'. with cpu, set index=0
        if device.find(':') == -1:
            device += ':' + str(torch.cuda.current_device())
    elif isinstance(device, int):
        device = 'cuda:' + str(device)
    elif isinstance(device, torch.device):
        if device.index is None:
            device = device.type + ':' + str(torch.cuda.current_device())
        else:
            device = device.type + ':' + str(device.index)
    else:
        raise wrap_exception(ORTModuleDeviceException, RuntimeError('Unsupported device type'))
    return device


def get_device_from_module(module):
    '''Returns the first device found in the `module`'s parameters or None

    Args:
        module (torch.nn.Module): PyTorch model to extract device from

    Raises:
        ORTModuleFallbackException: When more than one device is found at `module`
    '''
    device = None
    try:
        device = next(module.parameters()).device
        for param in module.parameters():
            if param.device != device:
                raise wrap_exception(ORTModuleDeviceException,
                                     RuntimeError('ORTModule supports a single device per model'))
    except StopIteration:
        # Model doesn't have a device set to any of the model parameters
        pass
    return device


def get_device_from_inputs(args, kwargs):
    '''Returns device from first PyTorch Tensor within args or kwargs

    Args:
        args: List with inputs
        kwargs: Dictionary with inputs
    '''

    device = None
    if args:
        device = torch.device(args[0].device)
    elif kwargs:
        device = torch.device(next(iter(kwargs.values())).device)
    return device


def _create_iobinding(io_binding, inputs, model, device):
    '''Creates IO binding for a `model` inputs and output'''
    for idx, value_info in enumerate(model.graph.input):
        io_binding.bind_ortvalue_input(value_info.name, OrtValue(_ortvalue_from_torch_tensor(inputs[idx])))

    for value_info in model.graph.output:
        io_binding.bind_output(value_info.name, device.type, device_id=get_device_index(device))

def check_for_name_collisions_and_bind_methods_to_ortmodule(ortmodule: torch.nn.Module,
                                                            user_module: torch.nn.Module):
    """Warns if there are any common attributes between the user's model and ORTModule and binds user methods to ORTModule

    If there are methods defined on the user's model that ORTModule does not recognize (custom methods),
    then this function binds these methods to ORTModule.

    Args:
        ortmodule: the ORTModule instance
        user_module: the user's torch.nn.Module

    Raises:
        UserWarning: If there are any overlapping attributes between the ortmodule and user_module (except forward)
    """

    ortmodule_attributes = dict(inspect.getmembers(ortmodule))
    torch_module_attributes = dict(inspect.getmembers(torch.nn.Module()))
    user_module_attributes = inspect.getmembers(user_module)

    # Check if any user defined attribute collides with ORTModule's attributes
    for attribute_name, attribute in user_module_attributes:
        if inspect.ismethod(attribute):
            # Skip the dunder methods
            if attribute_name.startswith('__'):
                continue

            # if the attribute is not a torch attribute, or if the torch attribute
            # corresponding to attribute_name is not a method or the user attribute
            # does not equal the torch attribute, then this is a user defined method.
            if attribute_name not in torch_module_attributes or \
                not inspect.ismethod(torch_module_attributes[attribute_name]) or \
                attribute.__func__ != torch_module_attributes[attribute_name].__func__:

                # forward is expected to be defined by the user.
                if attribute_name == 'forward':
                    continue

                # This is a user defined/overriden method. Check for collisions.
                if attribute_name in ortmodule_attributes:
                    # This is a user defined method, issue a warning.
                    warnings.warn(f"User Module's attribute name {attribute_name} collides with ORTModule's attribute name. "
                        "User Module's method may not be called upon invocation through ORTModule.")
                else:
                    # This is a custom method, copy it and bind the copy to ORTModule.
                    # This is needed for cases where the user's custom method invokes
                    # the forward method. It should go through ORTModule's forward implementation
                    # and not go through the user defined forward method.
                    ortmodule.__dict__[attribute_name] = types.MethodType(copy.deepcopy(attribute.__func__), ortmodule)
        else:
            if attribute_name not in torch_module_attributes and attribute_name in ortmodule_attributes:
                # This is a user defined attribute that collides with ORTModule
                if attribute_name in ortmodule_attributes:
                    warnings.warn(f"User Module's attribute name {attribute_name} collides with ORTModule's attribute name. "
                    "User Module's attribute may not be returned when trying to retrieve the attribute through ORTModule.")

def parse_os_env_skip_check_flags(env_name):
    """Returns a list of SkipChecks as defined by os env variable env_name"""

    return os.getenv(env_name).split('|')

def get_exception_as_string(exception):
    assert isinstance(exception, Exception), 'exception must be a `Exception`'

    try:
        raise exception
    except:
        return traceback.format_exc()

def switch_backend_to_pytorch(ortmodule, pytorch_module):
    ortmodule._torch_module = TorchModulePytorch(pytorch_module)

    # TODO: Rework by implementing the "__getattribute__" method.
    #       Assigning all default attributes from user's original torch.nn.Module into ORTModule
    ortmodule._backward_hooks = pytorch_module._backward_hooks
    ortmodule._forward_hooks = pytorch_module._forward_hooks
    ortmodule._forward_pre_hooks = pytorch_module._forward_pre_hooks
    ortmodule._parameters = pytorch_module._parameters
    ortmodule._buffers = pytorch_module._buffers
    ortmodule._non_persistent_buffers_set = pytorch_module._non_persistent_buffers_set
    ortmodule._is_full_backward_hook = pytorch_module._is_full_backward_hook
    ortmodule._state_dict_hooks = pytorch_module._state_dict_hooks
    ortmodule._load_state_dict_pre_hooks = pytorch_module._load_state_dict_pre_hooks
    ortmodule._modules = pytorch_module._modules
    ortmodule.forward = pytorch_module.forward

def warn_of_constant_inputs(data):
    warnings.warn(f"Received input of type {type(data)} which may be treated as a constant by ORT by default."
        " Please consider moving constant arguments to the model constructor.")
