import zarr 
import queue
import dask.array as da
import xarray as xr
import os
from pydantic import PositiveInt
import math
import logging
import shutil

logger = logging.getLogger(__name__)

class ZarrStore:
    def __init__(self, 
                 folder_path, 
                 size:list,
                 file_list:list,
                 channels:list, 
                 chunks: dict = {"time": 1, "lat": "auto", "lon": "auto"}, 
                 n_workers:PositiveInt=4):
        
        self.folder_path = folder_path
        self._num_timesteps = len(file_list)
        self._size = size  # (height, width)
        self._chunks = chunks
        self._num_timechunks = chunks.get("time", 1)
        self._num_ychunks = chunks.get("lat", size[0])
        self._num_xchunks = chunks.get("lon", size[1])
        self._n_workers = n_workers

        zarr_path , encoding = self.zarr_store_create(
            label='VIS', 
            channels=channels, 
            size=size
        )

        self.path = zarr_path

    def zarr_store_create(self, label, channels, size):
        def on_rm_error(func, path, exc_info):
            import stat

            # Change the file to be writable and try again
            os.chmod(path, stat.S_IWRITE)
            func(path)

        zarr_path = os.path.join(
            self.folder_path,
            f'MTG_FCI_{label}.zarr'
        )

        if os.path.exists(zarr_path):
            response = input(f"The folder '{zarr_path.split("/")[-1]}' already exists. Do you want to delete it? (yes/no): ").strip().lower()
            if response == 'yes':
                try:
                    shutil.rmtree(zarr_path, onexc=on_rm_error)
                    logger.info(f"Deleted existing folder: {zarr_path}")
                except Exception as e:
                    logger.error(f"Error deleting folder {zarr_path}: {e}")
                    raise FileExistsError(
                        f"Could not delete folder '{zarr_path.split('/')[-1]}'. Please delete it manually or choose a different folder."
                    )
            else:
                raise FileExistsError(
                    f"Folder '{zarr_path.split('/')[-1]}' already exists. Please choose a different folder or delete the existing one."
                )
            
                

        # Dimensions
        num_time = self._num_timesteps  
        height, width = size
        time_coord = list(range(num_time))

        all_vars = {
            "vis_06", "vis_08", "vis_09",
            "ir_105", "ir_123", "ir_133",
            "ir_38", "ir_87", "ir_97",
            "wv_63", "wv_73"
        }

        meta_vars = ["identifier", "unixTimeStart", "unixTimeEnd"]

        assert all(v in all_vars for v in channels), "One or more channels are invalid"
        selected_vars = set(channels)
        selected_vars.update(meta_vars)

        data_vars = {}
        encoding = {}
        compressor = zarr.Blosc(cname="zstd", clevel=4)

        for var in selected_vars:
            if var in meta_vars:
                shape = (num_time,)
                chunks = (self._num_timechunks,)
                dtype = 'S143' if var == "identifier" else "datetime64[ns]"
                dims = ("time",)
            else:
                shape = (num_time, height, width)
                chunks = (self._num_timechunks, self._num_ychunks, self._num_xchunks)
                dtype = 'float32' if var.startswith("vis_") else 'int32'
                dims = ("time", "lat", "lon")

            data_vars[var] = (dims, da.empty(shape, dtype=dtype, chunks=chunks))
            encoding[var] = {"compressor": compressor, 
                             "chunks": chunks}
    
        ds_empty = xr.Dataset(data_vars=data_vars, coords={"time": time_coord})
        ds_empty.to_zarr(zarr_path, mode='w', compute=True)

        return zarr_path, encoding


def compute_auto_chunks(shape, dtype_size=4, fixed_chunks={"time": 1}, target_chunk_bytes=128 * 2**20):
    """
    Compute approximate Dask chunk sizes for 'auto' chunks given fixed chunks along one dimension.
    
    Parameters:
    - shape: dict with dimension names and sizes, e.g., {"time": 1000, "lat": 2000, "lon": 2000}
    - dtype_size: size in bytes (e.g., 4 for float32, 8 for float64)
    - fixed_chunks: dict like {"time": 1}, others will be considered "auto"
    - target_chunk_bytes: default 128 MiB

    Returns:
    - dict with chunk sizes, including computed sizes for "auto" dimensions
    """
    # Separate fixed and auto dimensions
    fixed_volume = dtype_size
    chunk_shape = {}
    
    for dim, size in shape.items():
        if dim in fixed_chunks:
            chunk_shape[dim] = fixed_chunks[dim]
            fixed_volume *= fixed_chunks[dim]
        else:
            chunk_shape[dim] = None  # mark as "auto"

    # Remaining bytes for auto dimensions
    remaining_bytes = target_chunk_bytes / fixed_volume
    
    # Determine number of elements needed for auto dims
    auto_dims = [dim for dim in chunk_shape if chunk_shape[dim] is None]
    if len(auto_dims) != 2:
        raise ValueError("Function currently supports exactly 2 auto dimensions (e.g., lat, lon)")
    
    dim1, dim2 = auto_dims
    size1, size2 = shape[dim1], shape[dim2]

    # Solve: chunk1 * chunk2 ≈ remaining_elements
    target_elements = remaining_bytes / dtype_size
    chunk1 = int(math.sqrt(target_elements * size1 / size2))
    chunk2 = int(target_elements / chunk1)

    # Clip to max dimension size
    chunk_shape[dim1] = min(chunk1, size1)
    chunk_shape[dim2] = min(chunk2, size2)

    return chunk_shape