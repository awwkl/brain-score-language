import logging

from brainscore_language import data_registry

from brainscore_language.utils.s3 import load_from_s3
from brainscore_language.utils.local import load_from_disk

_logger = logging.getLogger(__name__)

BIBTEX = """@article{pereira2018toward,
  title={Toward a universal decoder of linguistic meaning from brain activation},
  author={Pereira, Francisco and Lou, Bin and Pritchett, Brianna and Ritter, Samuel and Gershman, Samuel J 
          and Kanwisher, Nancy and Botvinick, Matthew and Fedorenko, Evelina},
  journal={Nature communications},
  volume={9},
  number={1},
  pages={1--13},
  year={2018},
  publisher={Nature Publishing Group}
}"""


try:
    data_registry["Pereira2018_v2022.language"] = lambda: load_from_s3(
        identifier="Pereira2018ROI",
        # version_id="8kEzAXaWnxjMIC95MrhSJ3gtP2a1W.Y7",
        version_id="i1XqPs72b82kfzj9kZ9EQwVZjLltmlSz",
        # sha1="235ecf1f2abc38cb892c0a7b5c9c465434f153c5",
        sha1="d1dc23f2157cfcf6f78abf781a92480cd919ad1c",
        assembly_prefix="assembly_",
    )
except Exception as e:
    # TODO: check what class of Exception is thrown by S3 object not found, and catch that
    # while we don't know, we will continue to throw the exception rather than handling it
    raise e
    data_registry["Pereira2018_v2022.language"] = lambda: load_from_disk(
        "Pereira2018ROI"
    )
