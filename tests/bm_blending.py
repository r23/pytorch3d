# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.


from itertools import product

from fvcore.common.benchmark import benchmark
from test_blending import TestBlending


def bm_blending() -> None:
    devices = ["cuda"]
    kwargs_list = []
    num_meshes = [8]
    image_size = [64, 128, 256]
    faces_per_pixel = [50, 100]
    backend = ["pytorch", "custom"]
    test_cases = product(num_meshes, image_size, faces_per_pixel, devices, backend)

    for case in test_cases:
        n, s, k, d, b = case
        kwargs_list.append(
            {
                "num_meshes": n,
                "image_size": s,
                "faces_per_pixel": k,
                "device": d,
                "backend": b,
            }
        )

    benchmark(
        TestBlending.bm_sigmoid_alpha_blending,
        "SIGMOID_ALPHA_BLENDING_PYTORCH",
        kwargs_list,
        warmup_iters=1,
    )

    kwargs_list = [case for case in kwargs_list if case["backend"] == "pytorch"]
    benchmark(
        TestBlending.bm_softmax_blending,
        "SOFTMAX_BLENDING_PYTORCH",
        kwargs_list,
        warmup_iters=1,
    )
