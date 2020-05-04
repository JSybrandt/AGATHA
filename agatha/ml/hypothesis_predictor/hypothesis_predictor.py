from collections import defaultdict
import torch
import pytorch_lightning as pl
from argparse import Namespace, ArgumentParser
from agatha.ml.util.lamb_optimizer import Lamb
from typing import List, Tuple, Dict, Any
from agatha.util.entity_types import UMLS_TERM_TYPE, PREDICATE_TYPE
from agatha.util.sqlite3_lookup import Sqlite3Graph
from agatha.ml.util.embedding_lookup import EmbeddingLookupTable
from agatha.util.misc_util import iter_to_batches
from agatha.ml.hypothesis_predictor import predicate_util
from pathlib import Path
import os
from pytorch_lightning import Trainer
from agatha.ml.util import hparam_util

class HypothesisPredictor(pl.LightningModule):
  def __init__(self, hparams:Namespace):
    super(HypothesisPredictor, self).__init__()
    # If the hparams have been setup with paths, typical for training
    if (
        hasattr(hparams, "graph_db")
        and hasattr(hparams, "entity_db")
        and hasattr(hparams, "embedding_dir")
    ):
      self.configure_paths(
          graph_db=hparams.graph_db,
          entity_db=hparams.entity_db,
          embedding_dir=hparams.embedding_dir,
      )
    else: # Otherwise, the user will need to call configure_paths themselves
      self.graph = None
      self.embeddings = None
    # Clear paths, don't want to serialize them later
    self.hparams = hparam_util.remove_paths_from_namespace(hparams)

    # Set when init_process_group is called
    self._distributed = False

    # Layers
    ## Graph Emb Input
    self.embedding_transformation = torch.nn.Linear(
        self.hparams.dim, self.hparams.dim
    )
    ## Encoder Stack
    self.encode_predicate_data = torch.nn.TransformerEncoder(
        encoder_layer=torch.nn.TransformerEncoderLayer(
          self.hparams.dim,
          self.hparams.transformer_heads,
          self.hparams.transformer_ff_dim,
          self.hparams.transformer_dropout,
          "relu"
        ),
        num_layers=self.hparams.transformer_layers,
        norm=torch.nn.LayerNorm(self.hparams.dim),
    )
    ## Avg emb to logit
    self.encoding_to_logit = torch.nn.Linear(
      self.hparams.dim, 1
    )
    # Loss Fn
    self.loss_fn = torch.nn.MarginRankingLoss(margin=self.hparams.margin)

    # Helper data, set by prepare_for_training
    self.training_predicates = None
    self.validation_predicates = None
    self.predicates = None
    self.coded_terms = None
    self.predicate_batch_generator = None

  def _vprint(self, *args, **kwargs):
    if self.hparams.verbose:
      print(*args, **kwargs)

  def set_verbose(self, val:bool)->None:
    self.hparams.verbose = val

  def configure_paths(
      self,
      graph_db:Path,
      entity_db:Path,
      embedding_dir:Path,
  ):
    assert graph_db.is_file(), f"Failed to find {graph_db}"
    assert entity_db.is_file(), f"Failed to find {entity_db}"
    assert embedding_dir.is_dir(), f"Failed to find {embedding_dir}"
    self.embeddings = EmbeddingLookupTable(
        embedding_dir=embedding_dir,
        entity_db=entity_db,
    )
    self.graph=Sqlite3Graph(graph_db)

  def paths_set(self)->bool:
    return self.embeddings is not None and self.graph is not None

  def preload(self, include_embeddings:bool=False)->None:
    assert self.paths_set(), "Must call configure_paths before preload."
    if not self.is_preloaded():
      self.graph.preload()
      if include_embeddings:
        self.embeddings.preload()
      else:
        self.embeddings.entities.preload()

  def is_preloaded(self)->None:
    return (
        self.paths_set()
        and self.graph.is_preloaded()
        and self.embeddings.entities.is_preloaded()
    )

  def prepare_for_training(self)->None:
    assert self.paths_set(), \
        "Must call configure_paths before prepare_for_training"
    entities = self.embeddings.keys()
    assert len(entities) > 0, "Failed to find embedding entities."
    self.coded_terms = list(filter(
      lambda k: k[0] == UMLS_TERM_TYPE,
      entities
    ))
    self.predicates = list(filter(
      lambda k: k[0] == PREDICATE_TYPE,
      entities
    ))
    self._vprint("Splitting train/validation")
    validation_size = int(
        len(self.predicates) * self.hparams.validation_fraction
    )
    training_size = len(self.predicates) - validation_size
    self._vprint("\t- Training:", training_size)
    self._vprint("\t- Validation:", validation_size)
    (
        self.training_predicates,
        self.validation_predicates,
    ) = torch.utils.data.random_split(
        self.predicates,
        [training_size, validation_size]
    )
    self._vprint("Preparing Batch Generator")
    self.predicate_batch_generator = predicate_util.PredicateBatchGenerator(
        graph=self.graph,
        embeddings=self.embeddings,
        predicates=self.predicates,
        coded_terms=self.coded_terms,
        neighbor_sample_rate=self.hparams.neighbor_sample_rate,
        negative_swap_rate=self.hparams.negative_swap_rate,
        negative_scramble_rate=self.hparams.negative_scramble_rate,
    )
    self._vprint("Ready for training!")

  def _configure_dataloader(
      self,
      predicate_dataset:torch.utils.data.Dataset,
      shuffle:bool,
  )->torch.utils.data.DataLoader:
    self.preload()
    sampler = None
    if self._distributed:
      shuffle = False
      sampler=torch.utils.data.distributed.DistributedSampler(predicate_dataset)
    return torch.utils.data.DataLoader(
        dataset=predicate_dataset,
        shuffle=shuffle,
        sampler=sampler,
        batch_size=self.hparams.positives_per_batch,
        num_workers=self.hparams.dataloader_workers,
        pin_memory=True,
    )

  def train_dataloader(self)->torch.utils.data.DataLoader:
    self._vprint("Getting Training Dataloader")
    return self._configure_dataloader(
        self.training_predicates,
        shuffle=True,
    )

  def val_dataloader(self)->torch.utils.data.DataLoader:
    self._vprint("Getting Validation Dataloader")
    return self._configure_dataloader(
        self.validation_predicates,
        shuffle=False,
    )

  def forward(self, predicate_embeddings:torch.FloatTensor)->torch.FloatTensor:
    local_stacked_emb = self.embedding_transformation(predicate_embeddings)
    local_stacked_emb = torch.relu(local_stacked_emb)
    encoded_predicate = self.encode_predicate_data(local_stacked_emb)
    encoded_predicate = encoded_predicate.mean(dim=0)
    logit = self.encoding_to_logit(encoded_predicate)
    logit = torch.sigmoid(logit)
    return logit.reshape(-1)

  def get_device(self):
    return next(self.parameters()).device

  def _step(
      self,
      positive_predicates:List[str]
  )->Tuple[torch.Tensor, Dict[str, Any]]:
    """ Performs a forward pass of the model during training.

    Used in both training_step and validation_step, this function accepts a set
    of predicate names and performs a forward pass of the hypothesis generation
    training routine. This involves generating negative samples for each
    positive example and evaluating metrics that quantify the difference
    between the two.

    Args:
      positive_predicates: A list of predicate names, each in the form,
        p:subj:verb:obj

    Returns:
      The first element is the loss tensor, used for back propagation. The
      second element is a dict containing all extra metrics.

    """
    pos, negs = self.predicate_batch_generator.generate(positive_predicates)
    positive_predictions = self.forward(
        predicate_util
        .collate_predicate_embeddings(pos)
        .to(self.get_device())
    )
    partial_losses = []
    for neg in negs:
      negative_predictions = self.forward(
          predicate_util
          .collate_predicate_embeddings(neg)
          .to(self.get_device())
      )
      partial_losses.append(
          self.loss_fn(
            positive_predictions,
            negative_predictions,
            positive_predictions.new_ones(len(positive_predictions))
          )
      )
    loss=torch.mean(torch.stack(partial_losses))
    return (
        loss,
        dict(
        )
    )

  def training_step(
      self,
      positive_predictions:List[str],
      batch_idx:int
  )->Dict[str, Any]:
    loss, metrics = self._step(positive_predictions)
    return dict(
        loss=loss,
        progress_bar=metrics,
        log=metrics
    )

  def validation_step(
      self,
      positive_predictions:List[str],
      batch_idx:int
  )->Dict[str, Any]:
    loss, metrics = self._step(positive_predictions)
    val_metrics = {f"val_{k}": v for k, v in metrics.items()}
    val_metrics["val_loss"] = loss
    return val_metrics

  def _on_epoch_end(
      self,
      outputs:List[Dict[str,torch.Tensor]]
  )->Dict[str, Dict[str,torch.Tensor]]:
    keys = outputs[0].keys()
    return {
        key: torch.mean(torch.stack([o[key] for o in outputs]))
        for key in keys
    }

  def validation_epoch_end(self, outputs:List[Dict[str,torch.Tensor]]):
    return self._on_epoch_end(outputs)

  def configure_optimizers(self):
    self._vprint("Configuring optimizers")
    return Lamb(
        self.parameters(),
        lr=self.hparams.lr,
        weight_decay=self.hparams.weight_decay,
    )

  def on_train_start(self):
    pl.LightningModule.on_train_start(self)
    self._vprint("Training started")


  def optimizer_step(
      self,
      epoch_idx,
      batch_idx,
      optimizer,
      optimizer_idx,
      second_order_closure=None
  ):
    # warm up lr
    if  self.trainer.global_step < self.hparams.warmup_steps:
      lr_scale = min(
          1.,
          float(self.trainer.global_step + 1)/float(self.hparams.warmup_steps)
      )
      for pg in optimizer.param_groups:
        pg['lr'] = lr_scale * self.hparams.lr
    optimizer.step()
    optimizer.zero_grad()


  @staticmethod
  def add_argparse_args(parser:ArgumentParser)->ArgumentParser:
    """Used to add all model parameters to argparse

    This static function allows for the easy configuration of argparse for the
    construction and training of the Agatha deep learning model. Example usage:

    ```python3
    parser = HypothesisPredictor.add_argparse_args(ArgumentParser())
    args = parser.parse_args()
    trainer = Trainer.from_argparse_args(args)
    model = HypothesisPredictor(args)
    ```

    Note, many of the arguments, such as the location of training databases or
    the paths used to save the model during training, will _NOT_ be serialized
    with the model. These can be configured either from `args` directly after
    parsing, or through `configure_paths` after training.

    Args:
      parser: An argparse parser to be configured. Will receive all necessary
        training and model parameter flags.

    Returns:
      A reference to the input argument parser.

    """
    parser = Trainer.add_argparse_args(parser)
    parser.add_argument("--dataloader-workers", type=int)
    parser.add_argument("--dim", type=int)
    parser.add_argument("--embedding-dir", type=Path)
    parser.add_argument("--entity-db", type=Path)
    parser.add_argument("--graph-db", type=Path)
    parser.add_argument("--lr", type=float)
    parser.add_argument("--margin", type=float)
    parser.add_argument("--negative-scramble-rate", type=int)
    parser.add_argument("--negative-swap-rate", type=int)
    parser.add_argument("--neighbor-sample-rate", type=int)
    parser.add_argument("--positives-per-batch", type=int)
    parser.add_argument("--transformer-dropout", type=float)
    parser.add_argument("--transformer-ff-dim", type=int)
    parser.add_argument("--transformer-heads", type=int)
    parser.add_argument("--transformer-layers", type=int)
    parser.add_argument("--validation-fraction", type=float)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--warmup-steps", type=int)
    parser.add_argument("--weight-decay", type=float)
    return parser

  def init_ddp_connection(
      self,
      proc_rank:int,
      world_size:int,
      is_slurm_managing_tasks:bool=False
  )->None:
    """
    Override to define your custom way of setting up a distributed environment.
    Lightning's implementation uses env:// init by default and sets the first node as root
    for SLURM managed cluster.
    Args:
        proc_rank: The current process rank within the node.
        world_size: Number of GPUs being use across all nodes. (num_nodes * num_gpus).
        is_slurm_managing_tasks: is cluster managed by SLURM.
    """
    if is_slurm_managing_tasks:
      self._init_slurm_connection()

    if 'MASTER_ADDR' not in os.environ:
      log.warning("MASTER_ADDR environment variable is not defined. Set as localhost")
      os.environ['MASTER_ADDR'] = '127.0.0.1'

    if 'MASTER_PORT' not in os.environ:
      log.warning("MASTER_PORT environment variable is not defined. Set as 12910")
      os.environ['MASTER_PORT'] = '12910'

    if 'WORLD_SIZE' in os.environ and os.environ['WORLD_SIZE'] != world_size:
      log.warning("WORLD_SIZE environment variable is not equal to the computed "
                   "world size. Ignored.")

    # Reverting to GLOO until nccl upgrades in pytorch are complete
    #torch_backend = "nccl" if self.trainer.on_gpu else "gloo"
    torch_backend = "gloo"

    self._vprint(
        "Attempting connection:",
        torch_backend,
        os.environ["MASTER_ADDR"], proc_rank
    )
    self._distributed = True
    torch.distributed.init_process_group(
        torch_backend,
        rank=proc_rank,
        world_size=world_size
    )
