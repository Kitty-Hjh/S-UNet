from copy import deepcopy
from typing import Tuple, Union, List

import numpy as np
from batchgenerators.augmentations.utils import resize_segmentation
from nnunet.inference.segmentation_export import save_segmentation_nifti_from_softmax, save_segmentation_nifti
from batchgenerators.utilities.file_and_folder_operations import *
#from multiprocessing import Process, Queue
from queue import Queue

# from multiprocessing.dummy import Queue
import torch
import SimpleITK as sitk
import shutil

from nnunet.postprocessing.connected_components import load_remove_save, load_postprocessing
from nnunet.training.model_restore import load_model_and_checkpoint_files
from nnunet.training.network_training.nnUNetTrainer import nnUNetTrainer
from nnunet.utilities.one_hot_encoding import to_one_hot

import cc3d
import torch.nn.functional as F
import segmentation_models_pytorch as smp
import torchio
from einops import rearrange


def con_comp(seg_array):
    connectivity = 18
    conn_comp = cc3d.connected_components(seg_array, connectivity=connectivity)
    return conn_comp


def predict_2d(model_path, input_folder):
    model = smp.Unet(encoder_name='timm-res2net50_26w_4s',
                     encoder_weights=None,
                     encoder_depth=5, 
                     decoder_use_batchnorm=True,
                     decoder_channels=[320, 256, 128, 64, 32],
                     in_channels=5,
                     classes=2)
    
    model.load_state_dict(torch.load(join(model_path, 'fold_0/epoch_030.pth')))
    model.cuda()
    model.eval()
    transform = torchio.Compose([
        torchio.transforms.RescaleIntensity(out_min_max=(-1, 1), 
                                            in_min_max=(0, 35)),
    ])
    pet_path = os.path.join(input_folder, 'TCIA_001_0000.nii.gz')
    pet = torchio.ScalarImage(pet_path)
    pet = transform(pet)
    pet = pet.numpy()
   
    c, w, h, d = pet.shape
    output = np.zeros((w, 400, 400))  # final result

    value = 320
    upper = (400 - value) // 2
    down = upper + value

    value = 384
    left = (400 - value) // 2
    right = left + value

    # In Predicting
    batch_size = 16
    res = []
    for i in range(2, w-2, batch_size):
        if i + batch_size > w-2:
            start = i
            end = w-2
        else:
            start = i
            end = i + batch_size

        x0 = torch.tensor(pet[0, start-2:end-2 ])
        x1 = torch.tensor(pet[0, start-1:end-1 ])
        x2 = torch.tensor(pet[0, start  :end   ])
        x3 = torch.tensor(pet[0, start+1:end+1 ])
        x4 = torch.tensor(pet[0, start+2:end+2 ])

        x0 = x0.unsqueeze(1)
        x1 = x1.unsqueeze(1)
        x2 = x2.unsqueeze(1)
        x3 = x3.unsqueeze(1)
        x4 = x4.unsqueeze(1)

        pet_tensor = torch.cat([x0, x1, x2, x3, x4], dim=1)
        pet_tensor = F.interpolate(pet_tensor, (400, 400), mode='bilinear')
        pet_tensor = pet_tensor[:, :, upper:down, left:right]
        pet_tensor = pet_tensor.cuda()

        with torch.no_grad():
            r = model(pet_tensor)
        r = torch.softmax(r, dim=1)[:, 1:2]
        r = r.squeeze(1).cpu().numpy()
        res.append(r)

    out = np.concatenate(res, axis=0)
    output[2:-2, upper:down, left:right] = out
    result = torch.tensor(output, dtype=torch.float32)
    result = result.unsqueeze(0)
    result = F.interpolate(result, (h, d), mode='bilinear')
    
    return result[0] 
        
    
def check_input_folder_and_return_caseIDs(input_folder, expected_num_modalities):
    print("This model expects %d input modalities for each image" % expected_num_modalities)
    files = subfiles(input_folder, suffix=".nii.gz", join=False, sort=True)

    maybe_case_ids = np.unique([i[:-12] for i in files])

    remaining = deepcopy(files)
    missing = []

    assert len(files) > 0, "input folder did not contain any images (expected to find .nii.gz file endings)"

    # now check if all required files are present and that no unexpected files are remaining
    for c in maybe_case_ids:
        for n in range(expected_num_modalities):
            expected_output_file = c + "_%04.0d.nii.gz" % n
            if not isfile(join(input_folder, expected_output_file)):
                missing.append(expected_output_file)
            else:
                remaining.remove(expected_output_file)

    print("Found %d unique case ids, here are some examples:" % len(maybe_case_ids),
          np.random.choice(maybe_case_ids, min(len(maybe_case_ids), 10)))
    print("If they don't look right, make sure to double check your filenames. They must end with _0000.nii.gz etc")

    if len(remaining) > 0:
        print("found %d unexpected remaining files in the folder. Here are some examples:" % len(remaining),
              np.random.choice(remaining, min(len(remaining), 10)))

    if len(missing) > 0:
        print("Some files are missing:")
        print(missing)
        raise RuntimeError("missing files in input_folder")

    return maybe_case_ids


def predict_from_folder(model: str, input_folder: str, output_folder: str, folds: Union[Tuple[int], List[int]],
                        save_npz: bool, num_threads_preprocessing: int, num_threads_nifti_save: int,
                        lowres_segmentations: Union[str, None],
                        part_id: int, num_parts: int, tta: bool, mixed_precision: bool = True,
                        overwrite_existing: bool = True, mode: str = 'normal', overwrite_all_in_gpu: bool = None,
                        step_size: float = 0.5, checkpoint_name: str = "model_final_checkpoint",
                        segmentation_export_kwargs: dict = None, disable_postprocessing: bool = False):
    """
        here we use the standard naming scheme to generate list_of_lists and output_files needed by predict_cases
    :param model:
    :param input_folder:
    :param output_folder:
    :param folds:
    :param save_npz:
    :param num_threads_preprocessing:
    :param num_threads_nifti_save:
    :param lowres_segmentations:
    :param part_id:
    :param num_parts:
    :param tta:
    :param mixed_precision:
    :param overwrite_existing: if not None then it will be overwritten with whatever is in there. None is default (no overwrite)
    :return:
    """
    maybe_mkdir_p(output_folder)
    shutil.copy(join(model, 'plans.pkl'), output_folder)

    assert isfile(join(model, "plans.pkl")), "Folder with saved model weights must contain a plans.pkl file"
    expected_num_modalities = load_pickle(join(model, "plans.pkl"))['num_modalities']

    # check input folder integrity
    case_ids = check_input_folder_and_return_caseIDs(input_folder, expected_num_modalities)

    output_files = [join(output_folder, i + ".nii.gz") for i in case_ids]
    all_files = subfiles(input_folder, suffix=".nii.gz", join=False, sort=True)
    list_of_lists = [[join(input_folder, i) for i in all_files if i[:len(j)].startswith(j) and
                      len(i) == (len(j) + 12)] for j in case_ids]

    if lowres_segmentations is not None:
        assert isdir(lowres_segmentations), "if lowres_segmentations is not None then it must point to a directory"
        lowres_segmentations = [join(lowres_segmentations, i + ".nii.gz") for i in case_ids]
        assert all([isfile(i) for i in lowres_segmentations]), "not all lowres_segmentations files are present. " \
                                                               "(I was searching for case_id.nii.gz in that folder)"
        lowres_segmentations = lowres_segmentations[part_id::num_parts]
    else:
        lowres_segmentations = None

    if mode == "normal":
        if overwrite_all_in_gpu is None:
            all_in_gpu = False
        else:
            all_in_gpu = overwrite_all_in_gpu

        return predict_cases(model, list_of_lists[part_id::num_parts], output_files[part_id::num_parts], folds,
                             save_npz, num_threads_preprocessing, num_threads_nifti_save, lowres_segmentations, tta,
                             mixed_precision=mixed_precision, overwrite_existing=overwrite_existing,
                             all_in_gpu=all_in_gpu,
                             step_size=step_size, checkpoint_name=checkpoint_name,
                             segmentation_export_kwargs=segmentation_export_kwargs,
                             disable_postprocessing=disable_postprocessing)


def predict_cases(model, list_of_lists, output_filenames, folds, save_npz, num_threads_preprocessing,
                  num_threads_nifti_save, segs_from_prev_stage=None, do_tta=True, mixed_precision=True,
                  overwrite_existing=False,
                  all_in_gpu=False, step_size=0.5, checkpoint_name="model_final_checkpoint",
                  segmentation_export_kwargs: dict = None, disable_postprocessing: bool = False):
    """
    :param segmentation_export_kwargs:
    :param model: folder where the model is saved, must contain fold_x subfolders
    :param list_of_lists: [[case0_0000.nii.gz, case0_0001.nii.gz], [case1_0000.nii.gz, case1_0001.nii.gz], ...]
    :param output_filenames: [output_file_case0.nii.gz, output_file_case1.nii.gz, ...]
    :param folds: default: (0, 1, 2, 3, 4) (but can also be 'all' or a subset of the five folds, for example use (0, )
    for using only fold_0
    :param save_npz: default: False
    :param num_threads_preprocessing:
    :param num_threads_nifti_save:
    :param segs_from_prev_stage:
    :param do_tta: default: True, can be set to False for a 8x speedup at the cost of a reduced segmentation quality
    :param overwrite_existing: default: True
    :param mixed_precision: if None then we take no action. If True/False we overwrite what the model has in its init
    :return:
    """
    assert len(list_of_lists) == len(output_filenames)
    if segs_from_prev_stage is not None: assert len(segs_from_prev_stage) == len(output_filenames)

    results = []

    cleaned_output_files = []
    for o in output_filenames:
        dr, f = os.path.split(o)
        if len(dr) > 0:
            maybe_mkdir_p(dr)
        if not f.endswith(".nii.gz"):
            f, _ = os.path.splitext(f)
            f = f + ".nii.gz"
        cleaned_output_files.append(join(dr, f))

    if not overwrite_existing:
        print("number of cases:", len(list_of_lists))
        # if save_npz=True then we should also check for missing npz files
        not_done_idx = [i for i, j in enumerate(cleaned_output_files) if (not isfile(j)) or (save_npz and not isfile(j[:-7] + '.npz'))]

        cleaned_output_files = [cleaned_output_files[i] for i in not_done_idx]
        list_of_lists = [list_of_lists[i] for i in not_done_idx]
        if segs_from_prev_stage is not None:
            segs_from_prev_stage = [segs_from_prev_stage[i] for i in not_done_idx]

        print("number of cases that still need to be predicted:", len(cleaned_output_files))

    print("emptying cuda cache")
    torch.cuda.empty_cache()

    print("loading parameters for folds,", folds)
    trainer, params = load_model_and_checkpoint_files(model, folds, mixed_precision=mixed_precision,
                                                      checkpoint_name=checkpoint_name)

    if segmentation_export_kwargs is None:
        if 'segmentation_export_params' in trainer.plans.keys():
            force_separate_z = trainer.plans['segmentation_export_params']['force_separate_z']
            interpolation_order = trainer.plans['segmentation_export_params']['interpolation_order']
            interpolation_order_z = trainer.plans['segmentation_export_params']['interpolation_order_z']
        else:
            force_separate_z = None
            interpolation_order = 1
            interpolation_order_z = 0
    else:
        force_separate_z = segmentation_export_kwargs['force_separate_z']
        interpolation_order = segmentation_export_kwargs['interpolation_order']
        interpolation_order_z = segmentation_export_kwargs['interpolation_order_z']

    print("starting preprocessing generator")
    preprocessing = preprocess_multithreaded(trainer, list_of_lists, cleaned_output_files, num_threads_preprocessing,
                                             segs_from_prev_stage)

    print("starting prediction...")
    all_output_files = []
    for preprocessed in preprocessing:
        output_filename, (d, dct) = preprocessed
        all_output_files.append(all_output_files)
        if isinstance(d, str):
            data = np.load(d)
            os.remove(d)
            d = data

        print("predicting", output_filename)
        trainer.load_checkpoint_ram(params[0], False)
        softmax = trainer.predict_preprocessed_data_return_seg_and_softmax(
            d, do_mirroring=do_tta, mirror_axes=trainer.data_aug_params['mirror_axes'], use_sliding_window=True,
            step_size=step_size, use_gaussian=True, all_in_gpu=all_in_gpu,
            mixed_precision=mixed_precision)[1]

        for p in params[1:]:
            trainer.load_checkpoint_ram(p, False)
            softmax += trainer.predict_preprocessed_data_return_seg_and_softmax(
                d, do_mirroring=do_tta, mirror_axes=trainer.data_aug_params['mirror_axes'], use_sliding_window=True,
                step_size=step_size, use_gaussian=True, all_in_gpu=all_in_gpu,
                mixed_precision=mixed_precision)[1]

        if len(params) > 1:
            softmax /= len(params)

        transpose_forward = trainer.plans.get('transpose_forward')
        if transpose_forward is not None:
            transpose_backward = trainer.plans.get('transpose_backward')
            softmax = softmax.transpose([0] + [i + 1 for i in transpose_backward])
        
        softmax_2d = predict_2d(model, input_folder)
        softmax_2d = rearrange(softmax_2d, "w h d -> d h w").numpy()
        softmax_3d = softmax[1]
        result = np.zeros_like(softmax_3d)
        
        high = (softmax_3d > 0.90)
        low = (softmax_3d > 0.50)
        cue = (softmax_2d > 0.50)
        
        comp = con_comp(high)
        high_record = [0]
        for idx in range(1, comp.max() + 1):
            comp_mask = np.isin(comp, idx)
            high_record.append(np.sum(comp_mask))
        comp = con_comp(low)
        low_record = [0]
        for idx in range(1, comp.max() + 1):
            comp_mask = np.isin(comp, idx)
            low_record.append(np.sum(comp_mask))
        
        if np.max(high_record) < 50 and np.max(low_record) < 150:
            pass
        else:
            for idx in range(1, comp.max() + 1):
                comp_mask = np.isin(comp, idx)
                overlap_3d = np.sum(comp_mask * high)
                overlap_2d = np.max(comp_mask * cue)
                if overlap_3d < 20 or overlap_2d == 0:
                    result[(softmax_3d * comp_mask) == (softmax_3d * comp_mask).max()] = 1
                elif overlap_3d > 30000:
                    result = result + comp_mask * high
                else:
                    result = result + comp_mask
        
        if np.max(high_record) < 50 and np.max(low_record) < 150:
            pass
        else:
            comp = con_comp(cue)
            cue_record = [0]
            for idx in range(1, comp.max() + 1):
                comp_mask = np.isin(comp, idx)
                cue_record.append(np.sum(comp_mask))
            retain = len(low_record) + 6
            if len(cue_record) >= retain:
                cue_record.sort()
                area = cue_record[-retain]
            else:
                area = 35
            for idx in range(1, comp.max() + 1):
                comp_mask = np.isin(comp, idx)
                overlap = np.sum(comp_mask * result)
                if overlap > 0:
                    continue
                elif np.max(comp_mask * softmax_3d) > 0.10:
                    result[(softmax_3d * comp_mask) == (softmax_3d * comp_mask).max()] = 1
                elif np.sum(comp_mask) <= area:
                    continue
                elif np.max(comp_mask * softmax_3d) < 1e-5:
                    continue
                else:
                    result[(softmax_3d * comp_mask) == (softmax_3d * comp_mask).max()] = 1
        
        softmax[1] = result
        softmax[0] = 1 - result
        
        if save_npz:
            npz_file = output_filename[:-7] + ".npz"
        else:
            npz_file = None

        if hasattr(trainer, 'regions_class_order'):
            region_class_order = trainer.regions_class_order
        else:
            region_class_order = None

        """There is a problem with python process communication that prevents us from communicating objects 
        larger than 2 GB between processes (basically when the length of the pickle string that will be sent is 
        communicated by the multiprocessing.Pipe object then the placeholder (I think) does not allow for long 
        enough strings (lol). This could be fixed by changing i to l (for long) but that would require manually 
        patching system python code. We circumvent that problem here by saving softmax_pred to a npy file that will 
        then be read (and finally deleted) by the Process. save_segmentation_nifti_from_softmax can take either 
        filename or np.ndarray and will handle this automatically"""
        bytes_per_voxel = 4
        if all_in_gpu:
            bytes_per_voxel = 2  # if all_in_gpu then the return value is half (float16)
        if np.prod(softmax.shape) > (2e9 / bytes_per_voxel * 0.85):  # * 0.85 just to be save
            print(
                "This output is too large for python process-process communication. Saving output temporarily to disk")
            np.save(output_filename[:-7] + ".npy", softmax)
            softmax = output_filename[:-7] + ".npy"

        results.append(save_segmentation_nifti_from_softmax(softmax, output_filename, dct, interpolation_order,
                                                            region_class_order, None, None,
                                                            npz_file, None, force_separate_z, interpolation_order_z))

    print("inference done. Now waiting for the segmentation export to finish...")
    # _ = [i.get() for i in results]
    # now apply postprocessing
    # first load the postprocessing properties if they are present. Else raise a well visible warning
    if not disable_postprocessing:
        results = []
        pp_file = join(model, "postprocessing.json")
        if isfile(pp_file):
            print("postprocessing...")
            shutil.copy(pp_file, os.path.abspath(os.path.dirname(output_filenames[0])))
            # for_which_classes stores for which of the classes everything but the largest connected component needs to be
            # removed
            for_which_classes, min_valid_obj_size = load_postprocessing(pp_file)
            results.append(load_remove_save(zip(output_filenames, output_filenames,
                                                  [for_which_classes] * len(output_filenames),
                                                  [min_valid_obj_size] * len(output_filenames))))
            # _ = [i.get() for i in results]
        else:
            print("WARNING! Cannot run postprocessing because the postprocessing file is missing. Make sure to run "
                  "consolidate_folds in the output folder of the model first!\nThe folder you need to run this in is "
                  "%s" % model)

def preprocess_multithreaded(trainer, list_of_lists, output_files, num_processes=1, segs_from_prev_stage=None):
    if segs_from_prev_stage is None:
        segs_from_prev_stage = [None] * len(list_of_lists)

    num_processes = 1

    classes = list(range(1, trainer.num_classes))
    assert isinstance(trainer, nnUNetTrainer)
    q = Queue(2)
    preprocess_save_to_queue(trainer.preprocess_patient, q, list_of_lists, output_files, segs_from_prev_stage,
                             classes, trainer.plans['transpose_forward'])


    try:
        end_ctr = 0
        while end_ctr != num_processes:
            item = q.get()
            if item == "end":
                end_ctr += 1
                continue
            else:
                yield item

    finally:
        print('Preprocessing done')


def preprocess_save_to_queue(preprocess_fn, q, list_of_lists, output_files, segs_from_prev_stage, classes,
                             transpose_forward):
    # suppress output
    # sys.stdout = open(os.devnull, 'w')

    errors_in = []
    for i, l in enumerate(list_of_lists):
        try:
            output_file = output_files[i]
            print("preprocessing", output_file)
            d, _, dct = preprocess_fn(l)
            # print(output_file, dct)
            if segs_from_prev_stage[i] is not None:
                assert isfile(segs_from_prev_stage[i]) and segs_from_prev_stage[i].endswith(
                    ".nii.gz"), "segs_from_prev_stage" \
                                " must point to a " \
                                "segmentation file"
                seg_prev = sitk.GetArrayFromImage(sitk.ReadImage(segs_from_prev_stage[i]))
                # check to see if shapes match
                img = sitk.GetArrayFromImage(sitk.ReadImage(l[0]))
                assert all([i == j for i, j in zip(seg_prev.shape, img.shape)]), "image and segmentation from previous " \
                                                                                 "stage don't have the same pixel array " \
                                                                                 "shape! image: %s, seg_prev: %s" % \
                                                                                 (l[0], segs_from_prev_stage[i])
                seg_prev = seg_prev.transpose(transpose_forward)
                seg_reshaped = resize_segmentation(seg_prev, d.shape[1:], order=1)
                seg_reshaped = to_one_hot(seg_reshaped, classes)
                d = np.vstack((d, seg_reshaped)).astype(np.float32)
            """There is a problem with python process communication that prevents us from communicating objects 
            larger than 2 GB between processes (basically when the length of the pickle string that will be sent is 
            communicated by the multiprocessing.Pipe object then the placeholder (I think) does not allow for long 
            enough strings (lol). This could be fixed by changing i to l (for long) but that would require manually 
            patching system python code. We circumvent that problem here by saving softmax_pred to a npy file that will 
            then be read (and finally deleted) by the Process. save_segmentation_nifti_from_softmax can take either 
            filename or np.ndarray and will handle this automatically"""
            print(d.shape)
            if np.prod(d.shape) > (2e9 / 4 * 0.85):  # *0.85 just to be save, 4 because float32 is 4 bytes
                print(
                    "This output is too large for python process-process communication. "
                    "Saving output temporarily to disk")
                np.save(output_file[:-7] + ".npy", d)
                print("Saved")
                d = output_file[:-7] + ".npy"
            q.put((output_file, (d, dct)))
        except KeyboardInterrupt:
            raise KeyboardInterrupt
        except Exception as e:
            print("error in", l)
            print(e)
    q.put("end")
    if len(errors_in) > 0:
        print("There were some errors in the following cases:", errors_in)
        print("These cases were ignored.")
    else:
        print("This worker has ended successfully, no errors to report")
    # restore output
    # sys.stdout = sys.__stdout__
    
if __name__ == "__main__":
    model_folder_name = '/data2/hjh/upload/algorithm/checkpoints/nnUNet/3d_fullres/Task001_TCIA/nnUNetTrainerV2__nnUNetPlansv2.1/'
    input_folder = '/data2/clh/datasets/PETCT_Test/imagesTs'
    output_folder = '/data2/clh/datasets/PETCT_Test/predictions'
    
    folds = [0]
    save_npz = True
    num_threads_preprocessing = 1
    num_threads_nifti_save = 1
    lowres_segmentations = None
    part_id = 0
    num_parts = 1
    disable_tta = False
    overwrite_existing = True
    mode = 'normal'
    all_in_gpu = 'None'
    disable_mixed_precision = False
    step_size = 0.5
    chk = 'model_best'

    predict_from_folder(model_folder_name, input_folder, output_folder, folds, save_npz, num_threads_preprocessing,
                        num_threads_nifti_save, lowres_segmentations, part_id, num_parts, not disable_tta,
                        overwrite_existing=overwrite_existing, mode=mode, overwrite_all_in_gpu=all_in_gpu,
                        mixed_precision=not disable_mixed_precision,
                        step_size=step_size, checkpoint_name=chk)
