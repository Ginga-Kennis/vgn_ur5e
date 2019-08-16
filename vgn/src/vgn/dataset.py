from __future__ import print_function

import json
import os

import numpy as np
import torch.utils.data
from tqdm import tqdm

import vgn.config as cfg
from vgn import data, utils
from vgn.utils.transform import Rotation, Transform


class VGNDataset(torch.utils.data.Dataset):
    def __init__(self, root_dir, rebuild_cache=False):
        """Dataset for the volumetric grasping network.

        Args:
            root_dir: Path to the synthetic grasp dataset.
            rebuild_cache: Discard cached volumes.
        """
        self.root_dir = root_dir
        self.rebuild_cache = rebuild_cache

        self.detect_scenes()
        self.build_cache()

    def __len__(self):
        return len(self.scenes)

    def __getitem__(self, idx):
        scene = self.scenes[idx]
        data = np.load(os.path.join(self.cache_dir, scene) + '.npz')

        return data['tsdf'], data['indices'], data['scores']

    @property
    def cache_dir(self):
        return os.path.join(self.root_dir, 'cache')

    def detect_scenes(self):
        self.scenes = []
        for d in sorted(os.listdir(self.root_dir)):
            path = os.path.join(self.root_dir, d)
            if os.path.isdir(path) and path != self.cache_dir:
                self.scenes.append(d)

    def build_cache(self):
        print('Verifying cache:')

        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)

        for dirname in tqdm(self.scenes):

            path = os.path.join(self.cache_dir, dirname) + '.npz'
            if not os.path.exists(path) or self.rebuild_cache:
                scene = data.load_scene(os.path.join(self.root_dir, dirname))
                _, voxel_grid = data.reconstruct_volume(scene)
                tsdf = utils.voxel_grid_to_array(voxel_grid, cfg.resolution)

                indices = np.empty((len(scene['poses']), 3), dtype=np.long)
                scores = np.asarray(scene['scores'], dtype=np.float32)
                for i, pose in enumerate(scene['poses']):
                    index = voxel_grid.get_voxel(pose.translation)
                    indices[i] = np.clip(index, [0, 0, 0],
                                         [cfg.resolution - 1] * 3)

                np.savez_compressed(
                    path,
                    tsdf=np.expand_dims(tsdf, 0),
                    indices=indices,
                    scores=scores,
                )
