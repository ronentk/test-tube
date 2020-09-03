import contextlib
from pathlib import Path
import json
import os
import shutil
from datetime import datetime

import numpy as np
import pandas as pd
from imageio import imwrite
from tensorboard.compat.proto.event_pb2 import Event
from tensorboard.compat.proto.event_pb2 import SessionLog
from torch.utils.tensorboard import SummaryWriter, FileWriter

# constants
_ROOT = os.path.abspath(os.path.dirname(__file__))

# -----------------------------
# Experiment object
# -----------------------------


class DDPExperiment(object):
    def __init__(
        self,
        exp
    ):
        """
        Used as meta_data storage if the experiment needs to be pickled
        :param name:
        :param debug:
        :param version:
        :param save_dir:
        :param autosave:
        :param description:
        :param create_git_tag:
        :param args:
        :param kwargs:
        """

        self.tag_markdown_saved = exp.tag_markdown_saved
        self.no_save_dir = exp.no_save_dir
        self.metrics = exp.metrics
        self.tags = exp.tags
        self.name = exp.name
        self.debug = exp.debug
        self.version = exp.version
        self.autosave = exp.autosave
        self.description = exp.description
        self.create_git_tag = exp.create_git_tag
        self.exp_hash = exp.exp_hash
        self.created_at = exp.created_at
        self.save_dir = exp.save_dir


    def get_non_ddp_exp(self):
        return Experiment(
            name=self.name,
            debug=self.debug,
            version=self.version,
            save_dir=self.save_dir,
            autosave=self.autosave,
            description=self.description,
            create_git_tag=self.create_git_tag
        )

class Experiment(SummaryWriter):

    def __init__(
        self,
        save_dir=None,
        name='default',
        debug=False,
        version=None,
        autosave=False,
        description=None,
        create_git_tag=False,
        rank=0,
        *args, **kwargs
    ):
        """
        A new Experiment object defaults to 'default' unless a specific name is provided
        If a known name is already provided, then the file version is changed
        :param name:
        :param debug:
        """

        # change where the save dir is if requested

        if save_dir is not None:
            global _ROOT
            _ROOT = save_dir

        self.save_dir = save_dir
        self.tag_markdown_saved = False
        self.no_save_dir = save_dir is None
        self.metrics = []
        self.tags = {}
        self.name = name
        self.debug = debug
        self.version = version
        self.autosave = autosave
        self.description = description
        self.create_git_tag = create_git_tag
        self.exp_hash = '{}_v{}'.format(self.name, version)
        self.created_at = str(datetime.utcnow())
        self.rank = rank
        self.process = os.getpid()

        # when debugging don't do anything else
        if debug:
            return

        # update version hash if we need to increase version on our own
        # we will increase the previous version, so do it now so the hash
        # is accurate
        if version is None:
            old_version = self.__get_last_experiment_version()
            self.exp_hash = '{}_v{}'.format(self.name, old_version + 1)
            self.version = old_version + 1

        # create a new log file
        self.__init_cache_file_if_needed()

        # when we have a version, load it
        if self.version is not None:

            # when no version and no file, create it
            if not os.path.exists(self.__get_log_name()):
                self.__create_exp_file(self.version)
            else:
                # otherwise load it
                try:
                    self.__load()
                except Exception as e:
                    self.debug = True
        else:
            # if no version given, increase the version to a new exp
            # create the file if not exists
            old_version = self.__get_last_experiment_version()
            self.version = old_version
            self.__create_exp_file(self.version + 1)

        # create a git tag if requested
        if self.create_git_tag:
            desc = description if description is not None else 'no description'
            tag_msg = 'Test tube exp: {} - {}'.format(self.name, desc)
            cmd = 'git tag -a tt_{} -m "{}"'.format(self.exp_hash, tag_msg)
            os.system(cmd)
            print('Test tube created git tag:', 'tt_{}'.format(self.exp_hash))

        # set the tensorboardx log path to the /tf folder in the exp folder
        log_dir = self.get_tensorboardx_path(self.name, self.version)
        # this is a fix for pytorch 1.1 since it does not have this attribute
        for attr, val in [('purge_step', None),
                          ('max_queue', 10),
                          ('flush_secs', 120),
                          ('filename_suffix', '')]:
            if not hasattr(self, attr):
                setattr(self, attr, val)
        super().__init__(log_dir=log_dir, *args, **kwargs)

        # register on exit fx so we always close the writer
        # atexit.register(self.on_exit)

    def get_meta_copy(self):
        """
        Gets a meta-version only copy of this module
        :return:
        """
        return DDPExperiment(self)

    def on_exit(self):
        pass


    def __clean_dir(self):
        files = os.listdir(self.save_dir)

        if self.rank == 0:
            return

        for f in files:
            if str(self.process) in f:
                os.remove(os.path.join(self.save_dir, f))

    def argparse(self, argparser):
        parsed = vars(argparser)
        to_add = {}

        # don't store methods
        for k, v in parsed.items():
            if not callable(v):
                to_add[k] = v

        self.tag(to_add)

    def add_meta_from_hyperopt(self, hypo):
        """
        Transfers meta data about all the params from the
        hyperoptimizer to the log
        :param hypo:
        :return:
        """
        meta = hypo.get_current_trial_meta()
        for tag in meta:
            self.tag(tag)

    # --------------------------------
    # FILE IO UTILS
    # --------------------------------
    def __init_cache_file_if_needed(self):
        """
        Inits a file that we log historical experiments
        :return:
        """
        try:
            exp_cache_file = self.get_data_path(self.name, self.version)
            if not os.path.isdir(exp_cache_file):
                os.makedirs(exp_cache_file, exist_ok=True)
        except Exception as e:
            # file already exists (likely written by another exp. In this case disable the experiment
            self.debug = True

    def __create_exp_file(self, version):
        """
        Recreates the old file with this exp and version
        :param version:
        :return:
        """

        try:
            exp_cache_file = self.get_data_path(self.name, self.version)
            # if no exp, then make it
            path = '{}/meta.experiment'.format(exp_cache_file)
            open(path, 'w').close()
            self.version = version

            # make the directory for the experiment media assets name
            os.makedirs(self.get_media_path(self.name, self.version), exist_ok=True)

            # make the directory for tensorboardx stuff
            os.makedirs(self.get_tensorboardx_path(self.name, self.version), exist_ok=True)
        except Exception as e:
            # file already exists (likely written by another exp. In this case disable the experiment
            self.debug = True


    def __get_last_experiment_version(self):
        try:
            exp_cache_file = os.sep.join(self.get_data_path(self.name, self.version).split(os.sep)[:-1])
            return find_last_experiment_version(exp_cache_file)
        except Exception as e:
            return -1

    def __get_log_name(self):
        exp_cache_file = self.get_data_path(self.name, self.version)
        return '{}/meta.experiment'.format(exp_cache_file)

    def tag(self, tag_dict):
        """
        Adds a tag to the experiment.
        Tags are metadata for the exp.

        >> e.tag({"model": "Convnet A"})

        :param key:
        :param val:
        :return:
        """
        if self.debug or self.rank > 0: return

        # parse tags
        for k, v in tag_dict.items():
            self.tags[k] = v

        # save if needed
        if self.autosave == True:
            self.save()

    def log(self, metrics_dict, global_step=None, walltime=None):
        """
        Adds a json dict of metrics.

        >> e.log({"loss": 23, "coeff_a": 0.2})

        :param metrics_dict:
        :tag optional tfx tag
        :return:
        """
        if self.debug or self.rank > 0: return

        # handle tfx metrics
        if global_step is None:
            global_step = len(self.metrics)

        new_metrics_dict = metrics_dict.copy()
        for k, v in metrics_dict.items():
            if isinstance(v, dict):
                self.add_scalars(main_tag=k, tag_scalar_dict=v, global_step=global_step, walltime=walltime)
                tmp_metrics_dict = new_metrics_dict.pop(k)
                new_metrics_dict.update(tmp_metrics_dict)
            else:
                self.add_scalar(tag=k, scalar_value=v, global_step=global_step, walltime=walltime)

        metrics_dict = new_metrics_dict

        # timestamp
        if 'created_at' not in metrics_dict:
            metrics_dict['created_at'] = str(datetime.utcnow())

        self.__convert_numpy_types(metrics_dict)

        self.metrics.append(metrics_dict)

        if self.autosave:
            self.save()

    def __convert_numpy_types(self, metrics_dict):
        for k, v in metrics_dict.items():
            if v.__class__.__name__ == 'float32':
                metrics_dict[k] = float(v)

            if v.__class__.__name__ == 'float64':
                metrics_dict[k] = float(v)

    def save(self):
        """
        Saves current experiment progress
        :return:
        """
        if self.debug or self.rank > 0: return

        # save images and replace the image array with the
        # file name
        self.__save_images(self.metrics)
        metrics_file_path = self.get_data_path(self.name, self.version) + '/metrics.csv'
        meta_tags_path = self.get_data_path(self.name, self.version) + '/meta_tags.json'

        obj = {
            'name': self.name,
            'version': self.version,
            'tags_path': meta_tags_path,
            'metrics_path': metrics_file_path,
            'autosave': self.autosave,
            'description': self.description,
            'created_at': self.created_at,
            'exp_hash': self.exp_hash
        }

        # save the experiment meta file
        with atomic_write(self.__get_log_name()) as tmp_path:
            with open(tmp_path, 'w') as file:
                json.dump(obj, file, ensure_ascii=False)

        # save the metatags file
        with atomic_write(meta_tags_path) as tmp_path:
            f = Path(tmp_path)
            json.dump(self.tags, f.open(mode="w"))

        # save the metrics data
        df = pd.DataFrame(self.metrics)
        with atomic_write(metrics_file_path) as tmp_path:
            df.to_csv(tmp_path, index=False)

        # write new vals to disk
        self.flush()

        # until hparam plugin is fixed, generate hparams as text
        if not self.tag_markdown_saved and len(self.tags) > 0:
            self.tag_markdown_saved = True
            self.add_text('hparams', self.__generate_tfx_meta_log())

    def __generate_tfx_meta_log(self):
        header = f'''###### {self.name}, version {self.version}\n---\n'''
        desc = ''
        if self.description is not None:
            desc = f'''#####*{self.description}*\n'''
        params = f'''##### Hyperparameters\n'''

        row_header = '''parameter|value\n-|-\n'''
        rows = [row_header]
        for k, v in self.tags.items():
            row = f'''{k}|{v}\n'''
            rows.append(row)

        all_rows = [
            header,
            desc,
            params
        ]
        all_rows.extend(rows)
        mkdown_log = ''.join(all_rows)
        return mkdown_log

    def __save_images(self, metrics):
        """
        Save tags that have a png_ prefix (as images)
        and replace the meta tag with the file name
        :param metrics:
        :return:
        """
        # iterate all metrics and find keys with a specific prefix
        for i, metric in enumerate(metrics):
            for k, v in metric.items():
                # if the prefix is a png, save the image and replace the value with the path
                img_extension = None
                img_extension = 'png' if 'png_' in k else img_extension
                img_extension = 'jpg' if 'jpg' in k else img_extension
                img_extension = 'jpeg' if 'jpeg' in k else img_extension

                if img_extension is not None:
                    # determine the file name
                    img_name = '_'.join(k.split('_')[1:])
                    save_path = self.get_media_path(self.name, self.version)
                    save_path = '{}/{}_{}.{}'.format(save_path, img_name, i, img_extension)

                    # save image to disk
                    if type(metric[k]) is not str:
                        imwrite(save_path, metric[k])

                    # replace the image in the metric with the file path
                    metric[k] = save_path

    def __load(self):
        # load .experiment file
        with open(self.__get_log_name(), 'r') as file:
            data = json.load(file)
            self.name = data['name']
            self.version = data['version']
            self.autosave = data['autosave']
            self.created_at = data['created_at']
            self.description = data['description']
            self.exp_hash = data['exp_hash']

        # load .tags file
        meta_tags_path = Path(self.get_data_path(self.name, self.version) + '/meta_tags.json')
        try:
            tags = json.load(meta_tags_path.open())
        except ValueError: # failed to decode json
            tags = {}
        self.tags = tags
        # for d in self.tags_list:
        #     k, v = d['key'], d['value']
        #     self.tags[k] = v

        # load metrics
        metrics_file_path = self.get_data_path(self.name, self.version) + '/metrics.csv'
        try:
            df = pd.read_csv(metrics_file_path)
            self.metrics = df.to_dict(orient='records')

            # remove nans
            for metric in self.metrics:
                to_delete = []
                for k, v in metric.items():
                    try:
                        if np.isnan(v):
                            to_delete.append(k)
                    except Exception as e:
                        pass

                for k in to_delete:
                    del metric[k]
        except Exception as e:
            # metrics was empty...
            self.metrics = []

    def get_data_path(self, exp_name, exp_version):
        """
        Returns the path to the local package cache
        :param path:
        :return:
        """
        if self.no_save_dir:
            return os.path.join(_ROOT, 'test_tube_data', exp_name, 'version_{}'.format(exp_version))
        else:
            return os.path.join(_ROOT, exp_name, 'version_{}'.format(exp_version))

    def get_media_path(self, exp_name, exp_version):
        """
        Returns the path to the local package cache
        :param path:
        :return:
        """
        return os.path.join(self.get_data_path(exp_name, exp_version), 'media')

    def get_tensorboardx_path(self, exp_name, exp_version):
        """
        Returns the path to the local package cache
        :param path:
        :return:
        """
        return os.path.join(self.get_data_path(exp_name, exp_version), 'tf')

    def get_tensorboardx_scalars_path(self, exp_name, exp_version):
        """
        Returns the path to the local package cache
        :param path:
        :return:
        """
        tfx_path = self.get_tensorboardx_path(exp_name, exp_version)
        return os.path.join(tfx_path, 'scalars.json')


    # ----------------------------
    # OVERWRITES
    # ----------------------------
    def _get_file_writer(self):
        """Returns the default FileWriter instance. Recreates it if closed."""
        if self.rank > 0:
            return TTDummyFileWriter()

        if self.all_writers is None or self.file_writer is None:
            if self.purge_step is not None:
                most_recent_step = self.purge_step
                self.file_writer = FileWriter(self.log_dir, self.max_queue,
                                          self.flush_secs, self.filename_suffix)
                self.file_writer.debug = self.debug
                self.file_writer.rank = self.rank

                self.file_writer.add_event(
                    Event(step=most_recent_step, file_version='brain.Event:2'))
                self.file_writer.add_event(
                    Event(step=most_recent_step, session_log=SessionLog(status=SessionLog.START)))
            else:
                self.file_writer = FileWriter(self.log_dir, self.max_queue,
                                          self.flush_secs, self.filename_suffix)
            self.all_writers = {self.file_writer.get_logdir(): self.file_writer}
        return self.file_writer


    def __str__(self):
        return 'Exp: {}, v: {}'.format(self.name, self.version)

    def __hash__(self):
        return 'Exp: {}, v: {}'.format(self.name, self.version)

    def flush(self):
        if self.rank > 0:
            return

        if self.all_writers is None:
            return  # ignore double close

        for writer in self.all_writers.values():
            writer.flush()


class TTDummyFileWriter(object):

    def add_summary(self, summary, global_step=None, walltime=None):
        """
        Overwrite tf add summary so we can ignore when other non-zero processes call it
        Avoids overwriting logs from multiple processes
        :param summary:
        :param global_step:
        :param walltime:
        :return:
        """
        return


@contextlib.contextmanager
def atomic_write(dst_path):
    """A context manager to simplify atomic writing.

    Usage:
    >>> with atomic_write(dst_path) as tmp_path:
    >>>     # write to tmp_path
    >>> # Here tmp_path renamed to dst_path, if no exception happened.
    """
    tmp_path = str(dst_path) + '.tmp'
    try:
        yield tmp_path
    except:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
    else:
        # If everything is fine, move tmp file to the destination.
        shutil.move(tmp_path, str(dst_path))


def find_last_experiment_version(path):
    last_version = -1
    for f in os.listdir(path):
        if 'version_' in f:
            file_parts = f.split('_')
            version = int(file_parts[-1])
            last_version = max(last_version, version)
    return last_version


if __name__ == '__main__':
    from time import sleep
    e = Experiment(description='my description')
    e.tag({'lr': 0.02, 'layers': 4})

    for n_iter in range(20):
        sleep(0.3)
        e.log({'loss/xsinx': n_iter * np.sin(n_iter)})
        if n_iter % 10 == 0:
            print('saved')
            e.save()

    e.close()
    os._exit(1)

