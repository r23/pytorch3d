# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.


"""
This module implements utility functions for loading and saving
meshes and point clouds as PLY files.
"""
import itertools
import struct
import sys
import warnings
from collections import namedtuple
from io import BytesIO, TextIOBase
from pathlib import Path
from typing import List, Optional, Tuple, Union

import numpy as np
import torch
from iopath.common.file_io import PathManager
from pytorch3d.io.utils import _check_faces_indices, _make_tensor, _open_file
from pytorch3d.structures import Meshes, Pointclouds

from .pluggable_formats import (
    MeshFormatInterpreter,
    PointcloudFormatInterpreter,
    endswith,
)


_PlyTypeData = namedtuple("_PlyTypeData", "size struct_char np_type")

_PLY_TYPES = {
    "char": _PlyTypeData(1, "b", np.byte),
    "uchar": _PlyTypeData(1, "B", np.ubyte),
    "short": _PlyTypeData(2, "h", np.short),
    "ushort": _PlyTypeData(2, "H", np.ushort),
    "int": _PlyTypeData(4, "i", np.int32),
    "uint": _PlyTypeData(4, "I", np.uint32),
    "float": _PlyTypeData(4, "f", np.float32),
    "double": _PlyTypeData(8, "d", np.float64),
    "int8": _PlyTypeData(1, "b", np.byte),
    "uint8": _PlyTypeData(1, "B", np.ubyte),
    "int16": _PlyTypeData(2, "h", np.short),
    "uint16": _PlyTypeData(2, "H", np.ushort),
    "int32": _PlyTypeData(4, "i", np.int32),
    "uint32": _PlyTypeData(4, "I", np.uint32),
    "float32": _PlyTypeData(4, "f", np.float32),
    "float64": _PlyTypeData(8, "d", np.float64),
}

_Property = namedtuple("_Property", "name data_type list_size_type")


class _PlyElementType:
    """
    Description of an element of a Ply file.
    Members:
        self.properties: (List[_Property]) description of all the properties.
                            Each one contains a name and data type.
        self.count:      (int) number of such elements in the file
        self.name:       (str) name of the element
    """

    def __init__(self, name: str, count: int):
        self.name = name
        self.count = count
        self.properties = []

    def add_property(
        self, name: str, data_type: str, list_size_type: Optional[str] = None
    ):
        """Adds a new property.

        Args:
            name:           (str) name of the property.
            data_type:      (str) PLY data type.
            list_size_type: (str) PLY data type of the list size, or None if not
                            a list.
        """
        for property in self.properties:
            if property.name == name:
                msg = "Cannot have two properties called %s in %s."
                raise ValueError(msg % (name, self.name))
        self.properties.append(_Property(name, data_type, list_size_type))

    def is_fixed_size(self) -> bool:
        """Return whether the Element has no list properties

        Returns:
            True if none of the properties are lists.
        """
        for property in self.properties:
            if property.list_size_type is not None:
                return False
        return True

    def is_constant_type_fixed_size(self) -> bool:
        """Return whether the Element has all properties of the same non-list
        type.

        Returns:
            True if none of the properties are lists and all the properties
            share a type.
        """
        if not self.is_fixed_size():
            return False
        first_type = _PLY_TYPES[self.properties[0].data_type]
        for property in self.properties:
            if _PLY_TYPES[property.data_type] != first_type:
                return False
        return True

    def try_constant_list(self) -> bool:
        """Whether the element is just a single list, which might have a
        constant size, and therefore we could try to parse quickly with numpy.

        Returns:
            True if the only property is a list.
        """
        if len(self.properties) != 1:
            return False
        if self.properties[0].list_size_type is None:
            return False
        return True


class _PlyHeader:
    def __init__(self, f):
        """
        Load a header of a Ply file from a file-like object.
        Members:
            self.elements:   (List[_PlyElementType]) element description
            self.ascii:      (bool) Whether in ascii format
            self.big_endian: (bool) (if not ascii) whether big endian
            self.obj_info:   (List[str]) arbitrary extra data

        Args:
            f: file-like object.
        """
        if f.readline() not in [b"ply\n", b"ply\r\n", "ply\n"]:
            raise ValueError("Invalid file header.")
        seen_format = False
        self.elements = []
        self.obj_info = []
        while True:
            line = f.readline()
            if isinstance(line, bytes):
                line = line.decode("ascii")
            line = line.strip()
            if line == "end_header":
                if not self.elements:
                    raise ValueError("No elements found.")
                if not self.elements[-1].properties:
                    raise ValueError("Found an element with no properties.")
                if not seen_format:
                    raise ValueError("No format line found.")
                break
            if not seen_format:
                if line == "format ascii 1.0":
                    seen_format = True
                    self.ascii = True
                    continue
                if line == "format binary_little_endian 1.0":
                    seen_format = True
                    self.ascii = False
                    self.big_endian = False
                    continue
                if line == "format binary_big_endian 1.0":
                    seen_format = True
                    self.ascii = False
                    self.big_endian = True
                    continue
            if line.startswith("format"):
                raise ValueError("Invalid format line.")
            if line.startswith("comment") or len(line) == 0:
                continue
            if line.startswith("element"):
                self._parse_element(line)
                continue
            if line.startswith("obj_info "):
                self.obj_info.append(line[9:])
                continue
            if line.startswith("property"):
                self._parse_property(line)
                continue
            raise ValueError("Invalid line: %s." % line)

    def _parse_property(self, line: str):
        """
        Decode a ply file header property line.

        Args:
            line: (str) the ply file's line.
        """
        if not self.elements:
            raise ValueError("Encountered property before any element.")
        items = line.split(" ")
        if len(items) not in [3, 5]:
            raise ValueError("Invalid line: %s" % line)
        datatype = items[1]
        name = items[-1]
        if datatype == "list":
            datatype = items[3]
            list_size_type = items[2]
            if list_size_type not in _PLY_TYPES:
                raise ValueError("Invalid datatype: %s" % list_size_type)
        else:
            list_size_type = None
        if datatype not in _PLY_TYPES:
            raise ValueError("Invalid datatype: %s" % datatype)
        self.elements[-1].add_property(name, datatype, list_size_type)

    def _parse_element(self, line: str):
        """
        Decode a ply file header element line.

        Args:
            line: (str) the ply file's line.
        """
        if self.elements and not self.elements[-1].properties:
            raise ValueError("Found an element with no properties.")
        items = line.split(" ")
        if len(items) != 3:
            raise ValueError("Invalid line: %s" % line)
        try:
            count = int(items[2])
        except ValueError:
            msg = "Number of items for %s was not a number."
            raise ValueError(msg % items[1])
        self.elements.append(_PlyElementType(items[1], count))


def _read_ply_fixed_size_element_ascii(f, definition: _PlyElementType):
    """
    Given an element which has no lists and one type, read the
    corresponding data.

    For example

        element vertex 8
        property float x
        property float y
        property float z

    Args:
        f: file-like object being read.
        definition: The element object which describes what we are reading.

    Returns:
        1-element list containing a 2D numpy array corresponding to the data.
        The rows are the different values. There is one column for each property.
    """
    np_type = _PLY_TYPES[definition.properties[0].data_type].np_type
    old_offset = f.tell()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=".* Empty input file.*", category=UserWarning
        )
        data = np.loadtxt(
            f, dtype=np_type, comments=None, ndmin=2, max_rows=definition.count
        )
    if not len(data):  # np.loadtxt() seeks even on empty data
        f.seek(old_offset)
    if data.shape[1] != len(definition.properties):
        raise ValueError("Inconsistent data for %s." % definition.name)
    if data.shape[0] != definition.count:
        raise ValueError("Not enough data for %s." % definition.name)
    return [data]


def _read_ply_nolist_element_ascii(f, definition: _PlyElementType):
    """
    Given an element which has no lists and multiple types, read the
    corresponding data, by loading all the data as float64 and converting
    the relevant parts later.

    For example, given

        element vertex 8
        property float x
        property float y
        property float z
        property uchar red
        property uchar green
        property uchar blue

    the output will have two arrays, the first containing (x,y,z)
    and the second (red,green,blue).

    Args:
        f: file-like object being read.
        definition: The element object which describes what we are reading.

    Returns:
        List of 2D numpy arrays corresponding to the data.
    """
    old_offset = f.tell()
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", message=".* Empty input file.*", category=UserWarning
        )
        data = np.loadtxt(
            f, dtype=np.float64, comments=None, ndmin=2, max_rows=definition.count
        )
    if not len(data):  # np.loadtxt() seeks even on empty data
        f.seek(old_offset)
    if data.shape[1] != len(definition.properties):
        raise ValueError("Inconsistent data for %s." % definition.name)
    if data.shape[0] != definition.count:
        raise ValueError("Not enough data for %s." % definition.name)
    pieces = []
    offset = 0
    for dtype, it in itertools.groupby(p.data_type for p in definition.properties):
        count = sum(1 for _ in it)
        end_offset = offset + count
        piece = data[:, offset:end_offset].astype(_PLY_TYPES[dtype].np_type)
        pieces.append(piece)
        offset = end_offset
    return pieces


def _try_read_ply_constant_list_ascii(f, definition: _PlyElementType):
    """
    If definition is an element which is a single list, attempt to read the
    corresponding data assuming every value has the same length.
    If the data is ragged, return None and leave f undisturbed.

    For example, if the element is

        element face 2
        property list uchar int vertex_index

    and the data is

        4 0 1 2 3
        4 7 6 5 4

    then the function will return

        [[0, 1, 2, 3],
         [7, 6, 5, 4]]

    but if the data is

        4 0 1 2 3
        3 6 5 4

    then the function will return None.

    Args:
        f: file-like object being read.
        definition: The element object which describes what we are reading.

    Returns:
        If every element has the same size, 2D numpy array corresponding to the
        data. The rows are the different values. Otherwise None.
    """
    np_type = _PLY_TYPES[definition.properties[0].data_type].np_type
    old_offset = f.tell()
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message=".* Empty input file.*", category=UserWarning
            )
            data = np.loadtxt(
                f, dtype=np_type, comments=None, ndmin=2, max_rows=definition.count
            )
    except ValueError:
        f.seek(old_offset)
        return None
    if not len(data):  # np.loadtxt() seeks even on empty data
        f.seek(old_offset)
    if (data[:, 0] != data.shape[1] - 1).any():
        msg = "A line of %s data did not have the specified length."
        raise ValueError(msg % definition.name)
    if data.shape[0] != definition.count:
        raise ValueError("Not enough data for %s." % definition.name)
    return data[:, 1:]


def _parse_heterogeneous_property_ascii(datum, line_iter, property: _Property):
    """
    Read a general data property from an ascii .ply file.

    Args:
        datum: list to append the single value to. That value will be a numpy
                array if the property is a list property, otherwise an int or
                float.
        line_iter: iterator to words on the line from which we read.
        property: the property object describing the property we are reading.
    """
    value = next(line_iter, None)
    if value is None:
        raise ValueError("Too little data for an element.")
    if property.list_size_type is None:
        try:
            if property.data_type in ["double", "float"]:
                datum.append(float(value))
            else:
                datum.append(int(value))
        except ValueError:
            raise ValueError("Bad numerical data.")
    else:
        try:
            length = int(value)
        except ValueError:
            raise ValueError("A list length was not a number.")
        list_value = np.zeros(length, dtype=_PLY_TYPES[property.data_type].np_type)
        for i in range(length):
            inner_value = next(line_iter, None)
            if inner_value is None:
                raise ValueError("Too little data for an element.")
            try:
                list_value[i] = float(inner_value)
            except ValueError:
                raise ValueError("Bad numerical data.")
        datum.append(list_value)


def _read_ply_element_ascii(f, definition: _PlyElementType):
    """
    Decode all instances of a single element from an ascii .ply file.

    Args:
        f: file-like object being read.
        definition: The element object which describes what we are reading.

    Returns:
        In simple cases where every element has the same size, 2D numpy array
        corresponding to the data. The rows are the different values.
        Otherwise a list of lists of values, where the outer list is
        each occurrence of the element, and the inner lists have one value per
        property.
    """
    if not definition.count:
        return []
    if definition.is_constant_type_fixed_size():
        return _read_ply_fixed_size_element_ascii(f, definition)
    if definition.is_fixed_size():
        return _read_ply_nolist_element_ascii(f, definition)
    if definition.try_constant_list():
        data = _try_read_ply_constant_list_ascii(f, definition)
        if data is not None:
            return data

    # We failed to read the element as a lump, must process each line manually.
    data = []
    for _i in range(definition.count):
        line_string = f.readline()
        if line_string == "":
            raise ValueError("Not enough data for %s." % definition.name)
        datum = []
        line_iter = iter(line_string.strip().split())
        for property in definition.properties:
            _parse_heterogeneous_property_ascii(datum, line_iter, property)
        data.append(datum)
        if next(line_iter, None) is not None:
            raise ValueError("Too much data for an element.")
    return data


def _read_raw_array(f, aim: str, length: int, dtype=np.uint8, dtype_size=1):
    """
    Read [length] elements from a file.

    Args:
        f: file object
        aim: name of target for error message
        length: number of elements
        dtype: numpy type
        dtype_size: number of bytes per element.

    Returns:
        new numpy array
    """

    if isinstance(f, BytesIO):
        # np.fromfile is faster but won't work on a BytesIO
        needed_bytes = length * dtype_size
        bytes_data = bytearray(needed_bytes)
        n_bytes_read = f.readinto(bytes_data)
        if n_bytes_read != needed_bytes:
            raise ValueError("Not enough data for %s." % aim)
        data = np.frombuffer(bytes_data, dtype=dtype)
    else:
        data = np.fromfile(f, dtype=dtype, count=length)
        if data.shape[0] != length:
            raise ValueError("Not enough data for %s." % aim)
    return data


def _read_ply_fixed_size_element_binary(
    f, definition: _PlyElementType, big_endian: bool
):
    """
    Given an element which has no lists and one type, read the
    corresponding data.

    For example

        element vertex 8
        property float x
        property float y
        property float z


    Args:
        f: file-like object being read.
        definition: The element object which describes what we are reading.
        big_endian: (bool) whether the document is encoded as big endian.

    Returns:
        1-element list containing a 2D numpy array corresponding to the data.
        The rows are the different values. There is one column for each property.
    """
    ply_type = _PLY_TYPES[definition.properties[0].data_type]
    np_type = ply_type.np_type
    type_size = ply_type.size
    needed_length = definition.count * len(definition.properties)
    data = _read_raw_array(f, definition.name, needed_length, np_type, type_size)

    if (sys.byteorder == "big") != big_endian:
        data = data.byteswap()
    return [data.reshape(definition.count, len(definition.properties))]


def _read_ply_element_binary_nolists(f, definition: _PlyElementType, big_endian: bool):
    """
    Given an element which has no lists, read the corresponding data as tuple
    of numpy arrays, one for each set of adjacent columns with the same type.

    For example, given

        element vertex 8
        property float x
        property float y
        property float z
        property uchar red
        property uchar green
        property uchar blue

    the output will have two arrays, the first containing (x,y,z)
    and the second (red,green,blue).

    Args:
        f: file-like object being read.
        definition: The element object which describes what we are reading.
        big_endian: (bool) whether the document is encoded as big endian.

    Returns:
        List of 2D numpy arrays corresponding to the data. The rows are the different
        values.
    """
    size = sum(_PLY_TYPES[prop.data_type].size for prop in definition.properties)
    needed_bytes = size * definition.count
    data = _read_raw_array(f, definition.name, needed_bytes).reshape(-1, size)
    offset = 0
    pieces = []
    for dtype, it in itertools.groupby(p.data_type for p in definition.properties):
        count = sum(1 for _ in it)
        bytes_each = count * _PLY_TYPES[dtype].size
        end_offset = offset + bytes_each

        # what we want to do is
        # piece = data[:, offset:end_offset].view(_PLY_TYPES[dtype].np_type)
        # but it fails in the general case
        # because of https://github.com/numpy/numpy/issues/9496.
        piece = np.lib.stride_tricks.as_strided(
            data[:1, offset:end_offset].view(_PLY_TYPES[dtype].np_type),
            shape=(definition.count, count),
            strides=(data.strides[0], _PLY_TYPES[dtype].size),
        )

        if (sys.byteorder == "big") != big_endian:
            piece = piece.byteswap()
        pieces.append(piece)
        offset = end_offset
    return pieces


def _try_read_ply_constant_list_binary(
    f, definition: _PlyElementType, big_endian: bool
):
    """
    If definition is an element which is a single list, attempt to read the
    corresponding data assuming every value has the same length.
    If the data is ragged, return None and leave f undisturbed.

    For example, if the element is

        element face 2
        property list uchar int vertex_index

    and the data is

        4 0 1 2 3
        4 7 6 5 4

    then the function will return

        [[0, 1, 2, 3],
         [7, 6, 5, 4]]

    but if the data is

        4 0 1 2 3
        3 6 5 4

    then the function will return None.

    Args:
        f: file-like object being read.
        definition: The element object which describes what we are reading.
        big_endian: (bool) whether the document is encoded as big endian.

    Returns:
        If every element has the same size, 2D numpy array corresponding to the
        data. The rows are the different values. Otherwise None.
    """
    property = definition.properties[0]
    endian_str = ">" if big_endian else "<"
    length_format = endian_str + _PLY_TYPES[property.list_size_type].struct_char
    length_struct = struct.Struct(length_format)

    def get_length():
        bytes_data = f.read(length_struct.size)
        if len(bytes_data) != length_struct.size:
            raise ValueError("Not enough data for %s." % definition.name)
        [length] = length_struct.unpack(bytes_data)
        return length

    old_offset = f.tell()

    length = get_length()
    np_type = _PLY_TYPES[definition.properties[0].data_type].np_type
    type_size = _PLY_TYPES[definition.properties[0].data_type].size
    data_size = type_size * length

    output = np.zeros((definition.count, length), dtype=np_type)

    for i in range(definition.count):
        bytes_data = f.read(data_size)
        if len(bytes_data) != data_size:
            raise ValueError("Not enough data for %s" % definition.name)
        output[i] = np.frombuffer(bytes_data, dtype=np_type)
        if i + 1 == definition.count:
            break
        if length != get_length():
            f.seek(old_offset)
            return None
    if (sys.byteorder == "big") != big_endian:
        output = output.byteswap()

    return output


def _read_ply_element_binary(f, definition: _PlyElementType, big_endian: bool) -> list:
    """
    Decode all instances of a single element from a binary .ply file.

    Args:
        f: file-like object being read.
        definition: The element object which describes what we are reading.
        big_endian: (bool) whether the document is encoded as big endian.

    Returns:
        In simple cases where every element has the same size, 2D numpy array
        corresponding to the data. The rows are the different values.
        Otherwise a list of lists/tuples of values, where the outer list is
        each occurrence of the element, and the inner lists have one value per
        property.
    """
    if not definition.count:
        return []

    if definition.is_constant_type_fixed_size():
        return _read_ply_fixed_size_element_binary(f, definition, big_endian)
    if definition.is_fixed_size():
        return _read_ply_element_binary_nolists(f, definition, big_endian)
    if definition.try_constant_list():
        data = _try_read_ply_constant_list_binary(f, definition, big_endian)
        if data is not None:
            return data

    # We failed to read the element as a lump, must process each line manually.
    endian_str = ">" if big_endian else "<"
    property_structs = []
    for property in definition.properties:
        initial_type = property.list_size_type or property.data_type
        property_structs.append(
            struct.Struct(endian_str + _PLY_TYPES[initial_type].struct_char)
        )

    data = []
    for _i in range(definition.count):
        datum = []
        for property, property_struct in zip(definition.properties, property_structs):
            size = property_struct.size
            initial_data = f.read(size)
            if len(initial_data) != size:
                raise ValueError("Not enough data for %s" % definition.name)
            [initial] = property_struct.unpack(initial_data)
            if property.list_size_type is None:
                datum.append(initial)
            else:
                type_size = _PLY_TYPES[property.data_type].size
                needed_bytes = type_size * initial
                list_data = f.read(needed_bytes)
                if len(list_data) != needed_bytes:
                    raise ValueError("Not enough data for %s" % definition.name)
                np_type = _PLY_TYPES[property.data_type].np_type
                list_np = np.frombuffer(list_data, dtype=np_type)
                if (sys.byteorder == "big") != big_endian:
                    list_np = list_np.byteswap()
                datum.append(list_np)
        data.append(datum)
    return data


def _load_ply_raw_stream(f) -> Tuple[_PlyHeader, dict]:
    """
    Implementation for _load_ply_raw which takes a stream.

    Args:
        f:  A binary or text file-like object.

    Returns:
        header: A _PlyHeader object describing the metadata in the ply file.
        elements: A dictionary of element names to values. If an element is regular, in
        the sense of having no lists or being one uniformly-sized list, then the
        value will be a 2D numpy array. If not, it is a list of the relevant
        property values.
    """

    header = _PlyHeader(f)
    elements = {}
    if header.ascii:
        for element in header.elements:
            elements[element.name] = _read_ply_element_ascii(f, element)
    else:
        if isinstance(f, TextIOBase):
            raise ValueError(
                "Cannot safely read a binary ply file using a Text stream."
            )
        big = header.big_endian
        for element in header.elements:
            elements[element.name] = _read_ply_element_binary(f, element, big)
    end = f.read().strip()
    if len(end) != 0:
        raise ValueError("Extra data at end of file: " + str(end[:20]))
    return header, elements


def _load_ply_raw(f, path_manager: PathManager) -> Tuple[_PlyHeader, dict]:
    """
    Load the data from a .ply file.

    Args:
        f:  A binary or text file-like object (with methods read, readline,
            tell and seek), a pathlib path or a string containing a file name.
            If the ply file is binary, a text stream is not supported.
            It is recommended to use a binary stream.
        path_manager: PathManager for loading if f is a str.

    Returns:
        header: A _PlyHeader object describing the metadata in the ply file.
        elements: A dictionary of element names to values. If an element is
                  regular, in the sense of having no lists or being one
                  uniformly-sized list, then the value will be a 2D numpy array.
                  If it has no lists but more than one type, it will be a list of arrays.
                  If not, it is a list of the relevant property values.
    """
    with _open_file(f, path_manager, "rb") as f:
        header, elements = _load_ply_raw_stream(f)
    return header, elements


def _get_verts_column_indices(
    vertex_head: _PlyElementType,
) -> Tuple[List[int], Optional[List[int]]]:
    """
    Get the columns of verts and verts_colors in the vertex
    element of a parsed ply file.

    Args:
        vertex_head: as returned from load_ply_raw.

    Returns:
        point_idxs: List[int] of 3 point columns.
        color_idxs: List[int] of 3 color columns if they are present,
                    otherwise None.
    """
    point_idxs: List[Optional[int]] = [None, None, None]
    color_idxs: List[Optional[int]] = [None, None, None]
    for i, prop in enumerate(vertex_head.properties):
        if prop.list_size_type is not None:
            raise ValueError("Invalid vertices in file: did not expect list.")
        for j, letter in enumerate(["x", "y", "z"]):
            if prop.name == letter:
                point_idxs[j] = i
        for j, name in enumerate(["red", "green", "blue"]):
            if prop.name == name:
                color_idxs[j] = i
    if None in point_idxs:
        raise ValueError("Invalid vertices in file.")
    if None in color_idxs:
        return point_idxs, None
    return point_idxs, color_idxs


def _get_verts(
    header: _PlyHeader, elements: dict
) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
    """
    Get the vertex locations and colors from a parsed ply file.

    Args:
        header, elements: as returned from load_ply_raw.

    Returns:
        verts: FloatTensor of shape (V, 3).
        vertex_colors: None or FloatTensor of shape (V, 3).
    """

    vertex = elements.get("vertex", None)
    if vertex is None:
        raise ValueError("The ply file has no vertex element.")
    if not isinstance(vertex, list):
        raise ValueError("Invalid vertices in file.")
    vertex_head = next(head for head in header.elements if head.name == "vertex")
    point_idxs, color_idxs = _get_verts_column_indices(vertex_head)

    # Case of no vertices
    if vertex_head.count == 0:
        verts = torch.zeros((0, 3), dtype=torch.float32)
        if color_idxs is None:
            return verts, None
        return verts, torch.zeros((0, 3), dtype=torch.float32)

    # Simple case where the only data is the vertices themselves
    if (
        len(vertex) == 1
        and isinstance(vertex[0], np.ndarray)
        and vertex[0].ndim == 2
        and vertex[0].shape[1] == 3
    ):
        return _make_tensor(vertex[0], cols=3, dtype=torch.float32), None

    vertex_colors = None

    if len(vertex) == 1:
        # This is the case where the whole vertex element has one type,
        # so it was read as a single array and we can index straight into it.
        verts = torch.tensor(vertex[0][:, point_idxs], dtype=torch.float32)
        if color_idxs is not None:
            vertex_colors = torch.tensor(vertex[0][:, color_idxs], dtype=torch.float32)
    else:
        # The vertex element is heterogeneous. It was read as several arrays,
        # part by part, where a part is a set of properties with the same type.
        # For each property (=column in the file), we store in
        # prop_to_partnum_col its partnum (i.e. the index of what part it is
        # in) and its column number (its index within its part).
        prop_to_partnum_col = [
            (partnum, col)
            for partnum, array in enumerate(vertex)
            for col in range(array.shape[1])
        ]
        verts = torch.empty(size=(vertex_head.count, 3), dtype=torch.float32)
        for axis in range(3):
            partnum, col = prop_to_partnum_col[point_idxs[axis]]
            verts.numpy()[:, axis] = vertex[partnum][:, col]
            # Note that in the previous line, we made the assignment
            # as numpy arrays by casting verts. If we took the (more
            # obvious) method of converting the right hand side to
            # torch, then we might have an extra data copy because
            # torch wants contiguity. The code would be like:
            #   if not vertex[partnum].flags["C_CONTIGUOUS"]:
            #      vertex[partnum] = np.ascontiguousarray(vertex[partnum])
            #   verts[:, axis] = torch.tensor((vertex[partnum][:, col]))
        if color_idxs is not None:
            vertex_colors = torch.empty(
                size=(vertex_head.count, 3), dtype=torch.float32
            )
            for color in range(3):
                partnum, col = prop_to_partnum_col[color_idxs[color]]
                vertex_colors.numpy()[:, color] = vertex[partnum][:, col]

    return verts, vertex_colors


def _load_ply(
    f, *, path_manager: PathManager, return_vertex_colors: bool = False
) -> Tuple[torch.Tensor, torch.Tensor, Optional[torch.Tensor]]:
    """
    Load the data from a .ply file.

    Args:
        f:  A binary or text file-like object (with methods read, readline,
            tell and seek), a pathlib path or a string containing a file name.
            If the ply file is in the binary ply format rather than the text
            ply format, then a text stream is not supported.
            It is easiest to use a binary stream in all cases.
        path_manager: PathManager for loading if f is a str.
        return_vertex_colors: whether to return vertex colors.

    Returns:
        verts: FloatTensor of shape (V, 3).
        faces: None or LongTensor of vertex indices, shape (F, 3).
        vertex_colors: None or FloatTensor of shape (V, 3), only if requested
    """
    header, elements = _load_ply_raw(f, path_manager=path_manager)

    verts, vertex_colors = _get_verts(header, elements)

    face = elements.get("face", None)
    if face is not None:
        face_head = next(head for head in header.elements if head.name == "face")
        if (
            len(face_head.properties) != 1
            or face_head.properties[0].list_size_type is None
        ):
            raise ValueError("Unexpected form of faces data.")
        # face_head.properties[0].name is usually "vertex_index" or "vertex_indices"
        # but we don't need to enforce this.

    if face is None:
        faces = None
    elif not len(face):
        # pyre is happier when this condition is not joined to the
        # previous one with `or`.
        faces = None
    elif isinstance(face, np.ndarray) and face.ndim == 2:  # Homogeneous elements
        if face.shape[1] < 3:
            raise ValueError("Faces must have at least 3 vertices.")
        face_arrays = [face[:, [0, i + 1, i + 2]] for i in range(face.shape[1] - 2)]
        faces = torch.LongTensor(np.vstack(face_arrays))
    else:
        face_list = []
        for face_item in face:
            if face_item.ndim != 1:
                raise ValueError("Bad face data.")
            if face_item.shape[0] < 3:
                raise ValueError("Faces must have at least 3 vertices.")
            for i in range(face_item.shape[0] - 2):
                face_list.append([face_item[0], face_item[i + 1], face_item[i + 2]])
        faces = torch.tensor(face_list, dtype=torch.int64)

    if faces is not None:
        _check_faces_indices(faces, max_index=verts.shape[0])

    if return_vertex_colors:
        return verts, faces, vertex_colors
    return verts, faces, None


def load_ply(
    f, *, path_manager: Optional[PathManager] = None
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Load the data from a .ply file.

    Example .ply file format:

    ply
    format ascii 1.0           { ascii/binary, format version number }
    comment made by Greg Turk  { comments keyword specified, like all lines }
    comment this file is a cube
    element vertex 8           { define "vertex" element, 8 of them in file }
    property float x           { vertex contains float "x" coordinate }
    property float y           { y coordinate is also a vertex property }
    property float z           { z coordinate, too }
    element face 6             { there are 6 "face" elements in the file }
    property list uchar int vertex_index { "vertex_indices" is a list of ints }
    end_header                 { delimits the end of the header }
    0 0 0                      { start of vertex list }
    0 0 1
    0 1 1
    0 1 0
    1 0 0
    1 0 1
    1 1 1
    1 1 0
    4 0 1 2 3                  { start of face list }
    4 7 6 5 4
    4 0 4 5 1
    4 1 5 6 2
    4 2 6 7 3
    4 3 7 4 0

    Args:
        f:  A binary or text file-like object (with methods read, readline,
            tell and seek), a pathlib path or a string containing a file name.
            If the ply file is in the binary ply format rather than the text
            ply format, then a text stream is not supported.
            It is easiest to use a binary stream in all cases.
        path_manager: PathManager for loading if f is a str.

    Returns:
        verts: FloatTensor of shape (V, 3).
        faces: LongTensor of vertex indices, shape (F, 3).
    """

    if path_manager is None:
        path_manager = PathManager()
    verts, faces, _ = _load_ply(f, path_manager=path_manager)
    if faces is None:
        faces = torch.zeros(0, 3, dtype=torch.int64)

    return verts, faces


def _save_ply(
    f,
    *,
    verts: torch.Tensor,
    faces: Optional[torch.LongTensor],
    verts_normals: torch.Tensor,
    verts_colors: torch.Tensor,
    ascii: bool,
    decimal_places: Optional[int] = None,
) -> None:
    """
    Internal implementation for saving 3D data to a .ply file.

    Args:
        f: File object to which the 3D data should be written.
        verts: FloatTensor of shape (V, 3) giving vertex coordinates.
        faces: LongTensor of shape (F, 3) giving faces.
        verts_normals: FloatTensor of shape (V, 3) giving vertex normals.
        ascii: (bool) whether to use the ascii ply format.
        decimal_places: Number of decimal places for saving if ascii=True.
    """
    assert not len(verts) or (verts.dim() == 2 and verts.size(1) == 3)
    if faces is not None:
        assert not len(faces) or (faces.dim() == 2 and faces.size(1) == 3)
    assert not len(verts_normals) or (
        verts_normals.dim() == 2 and verts_normals.size(1) == 3
    )
    assert not len(verts_colors) or (
        verts_colors.dim() == 2 and verts_colors.size(1) == 3
    )

    if ascii:
        f.write(b"ply\nformat ascii 1.0\n")
    elif sys.byteorder == "big":
        f.write(b"ply\nformat binary_big_endian 1.0\n")
    else:
        f.write(b"ply\nformat binary_little_endian 1.0\n")
    f.write(f"element vertex {verts.shape[0]}\n".encode("ascii"))
    f.write(b"property float x\n")
    f.write(b"property float y\n")
    f.write(b"property float z\n")
    if verts_normals.numel() > 0:
        f.write(b"property float nx\n")
        f.write(b"property float ny\n")
        f.write(b"property float nz\n")
    if verts_colors.numel() > 0:
        f.write(b"property float red\n")
        f.write(b"property float green\n")
        f.write(b"property float blue\n")
    if len(verts) and faces is not None:
        f.write(f"element face {faces.shape[0]}\n".encode("ascii"))
        f.write(b"property list uchar int vertex_index\n")
    f.write(b"end_header\n")

    if not (len(verts)):
        warnings.warn("Empty 'verts' provided")
        return

    vert_data = torch.cat((verts, verts_normals, verts_colors), dim=1).detach().numpy()
    if ascii:
        if decimal_places is None:
            float_str = "%f"
        else:
            float_str = "%" + ".%df" % decimal_places
        np.savetxt(f, vert_data, float_str)
    else:
        assert vert_data.dtype == np.float32
        if isinstance(f, BytesIO):
            # tofile only works with real files, but is faster than this.
            f.write(vert_data.tobytes())
        else:
            vert_data.tofile(f)

    if faces is not None:
        faces_array = faces.detach().numpy()

        _check_faces_indices(faces, max_index=verts.shape[0])

        if len(faces_array):
            if ascii:
                np.savetxt(f, faces_array, "3 %d %d %d")
            else:
                # rows are 13 bytes: a one-byte 3 followed by three four-byte face indices.
                faces_uints = np.full((len(faces_array), 13), 3, dtype=np.uint8)
                faces_uints[:, 1:] = faces_array.astype(np.uint32).view(np.uint8)
                if isinstance(f, BytesIO):
                    f.write(faces_uints.tobytes())
                else:
                    faces_uints.tofile(f)


def save_ply(
    f,
    verts: torch.Tensor,
    faces: Optional[torch.LongTensor] = None,
    verts_normals: Optional[torch.Tensor] = None,
    ascii: bool = False,
    decimal_places: Optional[int] = None,
    path_manager: Optional[PathManager] = None,
) -> None:
    """
    Save a mesh to a .ply file.

    Args:
        f: File (or path) to which the mesh should be written.
        verts: FloatTensor of shape (V, 3) giving vertex coordinates.
        faces: LongTensor of shape (F, 3) giving faces.
        verts_normals: FloatTensor of shape (V, 3) giving vertex normals.
        ascii: (bool) whether to use the ascii ply format.
        decimal_places: Number of decimal places for saving if ascii=True.
        path_manager: PathManager for interpreting f if it is a str.

    """

    verts_normals = (
        torch.tensor([], dtype=torch.float32, device=verts.device)
        if verts_normals is None
        else verts_normals
    )

    if len(verts) and not (verts.dim() == 2 and verts.size(1) == 3):
        message = "Argument 'verts' should either be empty or of shape (num_verts, 3)."
        raise ValueError(message)

    if (
        faces is not None
        and len(faces)
        and not (faces.dim() == 2 and faces.size(1) == 3)
    ):
        message = "Argument 'faces' should either be empty or of shape (num_faces, 3)."
        raise ValueError(message)

    if len(verts_normals) and not (
        verts_normals.dim() == 2
        and verts_normals.size(1) == 3
        and verts_normals.size(0) == verts.size(0)
    ):
        message = "Argument 'verts_normals' should either be empty or of shape (num_verts, 3)."
        raise ValueError(message)

    verts_colors = torch.FloatTensor([])

    if path_manager is None:
        path_manager = PathManager()
    with _open_file(f, path_manager, "wb") as f:
        _save_ply(
            f,
            verts=verts,
            faces=faces,
            verts_normals=verts_normals,
            verts_colors=verts_colors,
            ascii=ascii,
            decimal_places=decimal_places,
        )


class MeshPlyFormat(MeshFormatInterpreter):
    def __init__(self):
        self.known_suffixes = (".ply",)

    def read(
        self,
        path: Union[str, Path],
        include_textures: bool,
        device,
        path_manager: PathManager,
        **kwargs,
    ) -> Optional[Meshes]:
        if not endswith(path, self.known_suffixes):
            return None

        verts, faces = load_ply(f=path, path_manager=path_manager)
        mesh = Meshes(verts=[verts.to(device)], faces=[faces.to(device)])
        return mesh

    def save(
        self,
        data: Meshes,
        path: Union[str, Path],
        path_manager: PathManager,
        binary: Optional[bool],
        decimal_places: Optional[int] = None,
        **kwargs,
    ) -> bool:
        if not endswith(path, self.known_suffixes):
            return False

        # TODO: normals are not saved. We only want to save them if they already exist.
        verts = data.verts_list()[0]
        faces = data.faces_list()[0]
        save_ply(
            f=path,
            verts=verts,
            faces=faces,
            ascii=binary is False,
            decimal_places=decimal_places,
            path_manager=path_manager,
        )
        return True


class PointcloudPlyFormat(PointcloudFormatInterpreter):
    def __init__(self):
        self.known_suffixes = (".ply",)

    def read(
        self,
        path: Union[str, Path],
        device,
        path_manager: PathManager,
        **kwargs,
    ) -> Optional[Pointclouds]:
        if not endswith(path, self.known_suffixes):
            return None

        verts, faces, features = _load_ply(
            f=path, path_manager=path_manager, return_vertex_colors=True
        )
        verts = verts.to(device)
        if features is None:
            pointcloud = Pointclouds(points=[verts])
        else:
            pointcloud = Pointclouds(points=[verts], features=[features.to(device)])
        return pointcloud

    def save(
        self,
        data: Pointclouds,
        path: Union[str, Path],
        path_manager: PathManager,
        binary: Optional[bool],
        decimal_places: Optional[int] = None,
        **kwargs,
    ) -> bool:
        if not endswith(path, self.known_suffixes):
            return False

        points = data.points_list()[0]
        features = data.features_list()[0]
        with _open_file(path, path_manager, "wb") as f:
            _save_ply(
                f=f,
                verts=points,
                verts_colors=features,
                verts_normals=torch.FloatTensor([]),
                faces=None,
                ascii=binary is False,
                decimal_places=decimal_places,
            )
        return True
