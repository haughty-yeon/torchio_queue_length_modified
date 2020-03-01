import typing
import warnings
import collections
from pathlib import Path
from typing import Union, Sequence, Optional, Any, TypeVar, Dict, List, Tuple
import torch
from torch.utils.data import Dataset
import numpy as np
from ..utils import get_stem
from ..torchio import DATA, AFFINE, TypePath
from .io import read_image, write_image


class Image:
    r"""Class to store information about an image.

    Args:
        name: String corresponding to the name of the image, e.g. ``t1``,
            or ``segmentation``.
        path: Path to a file that can be read by
            :mod:`SimpleITK` or :mod:`nibabel` or to a directory containing
            DICOM files.
        type\_: Type of image, such as :attr:`torchio.INTENSITY` or
            :attr:`torchio.LABEL`. This will be used by the transforms to
            decide whether to apply an operation, or which interpolation to use
            when resampling.
    """
    def __init__(self, name: str, path: TypePath, type_: str):
        self.name = name
        self.path = self._parse_path(path)
        self.type = type_

    def _parse_path(self, path: TypePath) -> Path:
        try:
            path = Path(path).expanduser()
        except TypeError:
            message = f'Conversion to path not possible for variable: {path}'
            raise TypeError(message)
        if not (path.is_file() or path.is_dir()):  # might be a dir with DICOM
            message = (
                f'File for image "{self.name}"'
                f' not found: "{path}"'
                )
            raise FileNotFoundError(message)
        return path

    def load(self, check_nans: bool = True) -> Tuple[torch.Tensor, np.ndarray]:
        r"""Load the image from disk.

        The file is expected to be monomodal and 3D. A channels dimension is
        added to the tensor.

        Args:
            check_nans: If ``True``, issues a warning if NaNs are found
                in the image

        Returns:
            Tuple containing a 4D data tensor of size
            :math:`(1, D_{in}, H_{in}, W_{in})`
            and a 2D 4x4 affine matrix
        """
        tensor, affine = read_image(self.path)
        tensor = tensor.unsqueeze(0)  # add channels dimension
        if check_nans and torch.isnan(tensor).any():
            warnings.warn(f'NaNs found in file "{self.path}"')
        return tensor, affine


class Subject(list):
    """Class to store information about the images corresponding to a subject.

    Args:
        *images: Instances of :class:`torchio.Image`.
        name: Subject ID
    """
    def __init__(self, *images: Image, name: str = ''):
        self._parse_images(images)
        super().__init__(images)
        self.name = name

    def __repr__(self):
        return f'{__class__.__name__}("{self.name}", {len(self)} images)'

    @staticmethod
    def _parse_images(images: Sequence[Image]) -> None:
        # Check that each element is a list
        if not isinstance(images, collections.abc.Sequence):
            message = (
                'Subject "images" parameter must be a sequence'
                f', not {type(images)}'
            )
            raise TypeError(message)

        # Check that it's not empty
        if not images:
            raise ValueError('Images list is empty')

        # Check that there are only instances of Image
        # and all images have different names
        names: List[str] = []
        for image in images:
            if not isinstance(image, Image):
                message = (
                    'Subject list elements must be instances of'
                    f' torchio.Image, not {type(image)}'
                )
                raise TypeError(message)
            if image.name in names:
                message = (
                    f'More than one image with name "{image.name}"'
                    ' found in images list'
                )
                raise KeyError(message)
            names.append(image.name)


class ImagesDataset(Dataset):
    """Base TorchIO dataset.

    :class:`ImagesDataset` is a reader of 3D medical images that directly
    inherits from :class:`torch.utils.Dataset`.
    It can be used with a :class:`torch.utils.DataLoader`
    for efficient loading and augmentation.
    It receives a list of subjects, where each subject is an instance of
    :class:`torchio.Subject` containing instances of :class:`torchio.Image`.
    The file format must be compatible with NiBabel or SimpleITK readers.
    It can also be a directory containing
    `DICOM <https://www.dicomstandard.org/>`_ files.

    Args:
        subjects: Sequence of instances of :class:`torchio.Subject`.
        transform: An instance of
            :class:`torchio.transforms.Transform` that is applied to each image
            after loading it.
        check_nans: If ``True``, issues a warning if NaNs are found
            in the image

    Example::

        >>> import torchio
        >>> from torchio import ImagesDataset, Image, Subject
        >>> subject_a = Subject([
        ...     Image('t1', '~/Dropbox/MRI/t1.nrrd', torchio.INTENSITY),
        ...     Image('label', '~/Dropbox/MRI/t1_seg.nii.gz', torchio.LABEL),
        >>> ])
        >>> subject_b = Subject(
        ...     Image('t1', '/tmp/colin27_t1_tal_lin.nii.gz', torchio.INTENSITY),
        ...     Image('t2', '/tmp/colin27_t2_tal_lin.nii', torchio.INTENSITY),
        ...     Image('label', '/tmp/colin27_seg1.nii.gz', torchio.LABEL),
        ... )
        >>> subjects_list = [subject_a, subject_b]
        >>> subjects_dataset = ImagesDataset(subjects_list)
        >>> subject_sample = subjects_dataset[0]
    """
    def __init__(
            self,
            subjects: Sequence[Subject],
            transform: Optional[Any] = None,
            check_nans: bool = True,
            ):
        self._parse_subjects_list(subjects)
        self.subjects = subjects
        self._transform = transform
        self.check_nans = check_nans

    def __len__(self):
        return len(self.subjects)

    def __getitem__(self, index: int) -> dict:
        subject = self.subjects[index]
        sample = {}
        for image in subject:
            tensor, affine = image.load(check_nans=self.check_nans)
            image_dict = {
                DATA: tensor,
                AFFINE: affine,
                'type': image.type,
                'path': str(image.path),
                'stem': get_stem(image.path),
            }
            sample[image.name] = image_dict

        # Apply transform (this is usually the bottleneck)
        if self._transform is not None:
            sample = self._transform(sample)
        return sample

    def set_transform(self, transform: Any) -> None:
        """Set the :attr:`transform` attribute.

        Args: an instance of :class:`torchio.transforms.Transform`
        """
        self._transform = transform

    @staticmethod
    def _parse_subjects_list(subjects_list: Sequence[Subject]) -> None:
        # Check that it's list or tuple
        if not isinstance(subjects_list, collections.abc.Sequence):
            raise TypeError(
                f'Subject list must be a sequence, not {type(subjects_list)}')

        # Check that it's not empty
        if not subjects_list:
            raise ValueError('Subjects list is empty')

        # Check each element
        for subject_list in subjects_list:
            Subject(*subject_list)

    @classmethod
    def save_sample(
            cls,
            sample: Dict[str, dict],
            output_paths_dict: Dict[str, TypePath],
            ) -> None:
        for key, output_path in output_paths_dict.items():
            tensor = sample[key][DATA][0]  # remove channels dim
            affine = sample[key][AFFINE]
            write_image(tensor, affine, output_path)
