import torch.utils.data as data
import numpy as np
import os
import json
import cv2
from PIL import Image
import data_utils
from augmentation import crop_or_padding_to_fixed_size, rotate_instance, crop_resize_instance_v1
import random
import torch


def resize_binary_map(binary_map, size):
    binary_map_tmp = []
    binary_map_shape = binary_map.shape
    if len(binary_map_shape) == 2:
        binary_map = np.expand_dims(binary_map, -1)
    for i in range(binary_map.shape[-1]):
        bm = binary_map[:, :, i]
        bm = cv2.resize(bm.astype('uint8'), size, \
                interpolation=cv2.INTER_NEAREST).astype('bool')
        binary_map_tmp.append(bm)
    binary_map = np.stack(binary_map_tmp, axis=-1)
    if len(binary_map_shape) == 2:
        binary_map = np.squeeze(binary_map)
    return binary_map


class Dataset(data.Dataset):

    def __init__(self, args, lr=False, transforms=None):
        super(Dataset, self).__init__()

        self.args = args
        self.height = args.image_height
        self.width = args.image_width
        self.lr = lr
        self.split = args.split

        self.load_cam_params()

        self.rotate_min = -30
        self.rotate_max = 30
        self.resize_ratio_min = 0.8
        self.resize_ratio_max = 1.2
        self.overlap_ratio = 1.0

        self.stereobj_root = args.data
        self.stereobj_data_root = self.stereobj_root
        self.cls_type = args.cls_type

        kp_filename = os.path.join(self.stereobj_root, 'objects', self.cls_type + '.kp')
        with open (kp_filename, 'r') as f:
            self.kps = f.read().split()
            self.kps = np.array([float(k) for k in self.kps])
            self.kps = np.reshape(self.kps, [-1, 3])

        split_filename = os.path.join(self.stereobj_root, 'split', self.split + '_' + self.cls_type + '.json')
        with open(split_filename, 'r') as f:
            filename_dict = json.load(f)

        self.filenames = []
        for subdir in filename_dict:
            for img_id in filename_dict[subdir]:
                self.filenames.append([subdir, img_id])
        self.filenames.sort()

        self._transforms = transforms
        self.num_kp = args.num_kp

    def load_cam_params(self):
        cam_param_filename = os.path.join(self.args.data, 'camera.json')
        with open(cam_param_filename, 'r') as f:
            cam_param = json.load(f)

        self.proj_matrix_l = np.array(cam_param['left']['P'])
        self.proj_matrix_r = np.array(cam_param['right']['P'])

        self.baseline = abs(self.proj_matrix_r[0, -1] / self.proj_matrix_r[0, 0])

    def read_data(self, img_id):

        path = os.path.join(self.stereobj_data_root, \
                img_id[0], img_id[1] + '.jpg')
        inp = Image.open(path)
        inp = inp.resize((2 * self.width, self.height))
        inp = np.asarray(inp)
        inp_l = inp[:, :self.width]
        ##### whether to read and process both left and right stereo images
        if self.lr:
            inp_r = inp[:, self.width:]

        if self.split != 'test':
            path = os.path.join(self.stereobj_data_root, \
                    img_id[0], img_id[1] + '_rt_label.json')
            with open(path, 'r') as f:
                rt_data = json.load(f)
            rt = None
            for obj in rt_data['class']:
                if rt_data['class'][obj] == self.cls_type:
                    rt = rt_data['rt'][obj]
                    break
            assert(rt is not None)
            R = np.array(rt['R'])
            t = np.array(rt['t'])
            cam_mat = self.proj_matrix_l[:, :-1]

            kps = np.dot(self.kps, R.T) + t
            kps_2d, _ = cv2.projectPoints(objectPoints=kps, \
                    rvec=np.zeros(shape=[3]), tvec=np.zeros(shape=[3]), \
                    cameraMatrix=cam_mat, distCoeffs=None)
            kps_2d[:, :, 0] = kps_2d[:, :, 0] / 1440 * self.width
            kps_2d[:, :, 1] = kps_2d[:, :, 1] / 1440 * self.height
            kps_2d = kps_2d[:, 0]
            kps_2d = kps_2d[:self.num_kp]
            kps_2d_l = np.copy(kps_2d)

            if self.lr:
                kps = np.dot(self.kps, R.T) + t + np.array([-self.baseline, 0, 0])
                kps_2d, _ = cv2.projectPoints(objectPoints=kps, \
                        rvec=np.zeros(shape=[3]), tvec=np.zeros(shape=[3]), \
                        cameraMatrix=cam_mat, distCoeffs=None)
                kps_2d[:, :, 0] = kps_2d[:, :, 0] / 1440 * self.width
                kps_2d[:, :, 1] = kps_2d[:, :, 1] / 1440 * self.height
                kps_2d = kps_2d[:, 0]
                kps_2d = kps_2d[:self.num_kp]
                kps_2d_r = np.copy(kps_2d)

            path = os.path.join(self.stereobj_data_root, \
                    img_id[0], img_id[1] + '_mask_label.npz')
            obj_mask = np.load(path)['masks'].item()
            ##### decode instance mask
            mask = np.zeros([1440, 1440], dtype='bool')

            mask_in_bbox = obj_mask['left'][obj]['mask']
            x_min = obj_mask['left'][obj]['x_min']
            x_max = obj_mask['left'][obj]['x_max']
            y_min = obj_mask['left'][obj]['y_min']
            y_max = obj_mask['left'][obj]['y_max']

            if x_min is not None:
                mask[y_min:(y_max+1), x_min:(x_max+1)] = mask_in_bbox
            mask = resize_binary_map(mask, (self.width, self.height))
            mask = mask.astype('uint8')
        else:
            kps_2d_l, kps_2d_r, mask, R, t = [], [], [], [], []

        if self.lr:
            return inp_l, inp_r, kps_2d_l, kps_2d_r, mask, R, t
        else:
            return inp_l, kps_2d_l, mask, R, t

    def __getitem__(self, index_tuple):
        if self.lr:
            return self.get_item_lr(index_tuple)
        else:
            return self.get_item_l(index_tuple)

    def get_item_l(self, index_tuple):
        # index, height, width = index_tuple
        index = index_tuple
        img_id = self.filenames[index]

        inp, kpt_2d, mask, R_gt, t_gt = self.read_data(img_id)

        view = False
        if view:
            import matplotlib.pyplot as plt
            plt.imshow(inp_l / 255.)
            plt.plot(kpt_2d_l[:, 0], kpt_2d_l[:, 1], 'ro')
            plt.figure()
            plt.imshow(inp_r / 255.)
            plt.plot(kpt_2d_r[:, 0], kpt_2d_r[:, 1], 'ro')
            plt.figure()
            plt.imshow(mask)
            plt.show()
            exit()

        if self.split != 'test':
            pose_gt = np.concatenate([R_gt, np.expand_dims(t_gt, -1)], axis=-1)
            if self._transforms is not None:
                inp, kpt_2d, mask, K = self._transforms(inp, kpt_2d, mask, self.proj_matrix_l)
            mask.astype(np.uint8)

            prob = data_utils.compute_prob(mask, kpt_2d)
        else:
            pose_gt = []
            prob = []

        ret = {'inp': inp, 'mask': mask, 'prob': prob, \
               'uv': kpt_2d, 'img_id': img_id, 'meta': {}, \
               'kpt_3d': self.kps[:self.num_kp], 'baseline': self.baseline, \
               'K': self.proj_matrix_l[:, :-1], 'pose_gt': pose_gt}
        return ret

    def get_item_lr(self, index_tuple):
        index = index_tuple
        img_id = self.filenames[index]

        inp_l, inp_r, kpt_2d_l, kpt_2d_r, mask, R_gt, t_gt = self.read_data(img_id)

        view = False
        if view:
            import matplotlib.pyplot as plt
            plt.imshow(inp_l / 255.)
            plt.plot(kpt_2d_l[:, 0], kpt_2d_l[:, 1], 'ro')
            plt.figure()
            plt.imshow(inp_r / 255.)
            plt.plot(kpt_2d_r[:, 0], kpt_2d_r[:, 1], 'ro')
            plt.figure()
            plt.imshow(mask)
            plt.show()
            exit()

        if self._transforms is not None:
            inp_l, kpt_2d_l, mask, K = self._transforms(inp_l, kpt_2d_l, mask, self.proj_matrix_l)
            inp_r, kpt_2d_r, mask, K = self._transforms(inp_r, kpt_2d_r, mask, self.proj_matrix_r)
            mask.astype(np.uint8)

        if self.split != 'test':
            pose_gt = np.concatenate([R_gt, np.expand_dims(t_gt, -1)], axis=-1)
            prob = data_utils.compute_prob(mask, kpt_2d_l)
        else:
            pose_gt = []
            prob = []

        ret = {'inp_l': inp_l, 'inp_r': inp_r, 'mask': mask, 'prob': prob, \
               'uv_l': kpt_2d_l, 'uv_r': kpt_2d_r, 'img_id': img_id, 'meta': {}, \
               'kpt_3d': self.kps[:self.num_kp], 'baseline': self.baseline, \
               'K': self.proj_matrix_l[:, :-1], 'pose_gt': pose_gt}
        return ret

    def __len__(self):
        return len(self.filenames)

    def augment(self, img, mask, kpt_2d, height, width):
        # add one column to kpt_2d for convenience to calculate
        hcoords = np.concatenate((kpt_2d, np.ones((self.num_kp, 1))), axis=-1)
        img = np.asarray(img).astype(np.uint8)
        foreground = np.sum(mask)
        # randomly mask out to add occlusion
        if foreground > 0:
            img, mask, hcoords = rotate_instance(img, mask, hcoords, self.rotate_min, self.rotate_max)
            img, mask, hcoords = crop_resize_instance_v1(img, mask, hcoords, height, width,
                                                         self.overlap_ratio,
                                                         self.resize_ratio_min,
                                                         self.resize_ratio_max)
        else:
            img, mask = crop_or_padding_to_fixed_size(img, mask, height, width)
        kpt_2d = hcoords[:, :2]

        return img, kpt_2d, mask

if __name__ == '__main__':
    from transforms import make_transforms
    transforms = make_transforms(True)

    dataset = Dataset(is_train=True, cls_type='hammer', num_kp=20, height=512, width=512, transforms=transforms)
    data = dataset[600]
    print(data)




