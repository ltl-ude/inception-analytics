import cassis
import pandas as pd
from itertools import combinations
from functools import lru_cache
from sklearn.metrics import cohen_kappa_score
from krippendorff import alpha

from utils import extend_layer_name, annotation_info_from_xmi_zip


class Project:
    @classmethod
    def from_zipped_xmi(cls, project_zip):
        annotations = annotation_info_from_xmi_zip(project_zip)
        return cls(annotations, project_zip, 'xmi')

    def __init__(self, annotations, project_path, export_format):
        self._annotation_info = pd.DataFrame(annotations, columns=['cas', 'source_file', 'annotator'])
        self.path = project_path
        self.export_format = export_format

    @property
    def typesystem(self):
        return self._annotation_info.loc[0, 'cas'].typesystem

    @property
    def source_file_names(self):
        return self._unique_entries('source_file')

    @property
    def annotators(self):
        return self._unique_entries('annotator')

    @property
    def cas_objects(self):
        return self._annotation_info['cas'].tolist()

    def _unique_entries(self, info_type):
        return self._annotation_info[info_type].unique().tolist()

    def _filter_annotation_info(self, annotators=None, source_files=None):
        df = self._annotation_info

        if annotators:
            df = df.query('annotator == @annotators')

        if source_files:
            df = df.query('source_file == @source_files')

        return df

    @lru_cache(maxsize=4)
    def view(self, layer_name, feature_name=None, annotators=None, source_files=None):
        level = 'layer' if feature_name is None else 'feature'
        return View(self.annotations(layer_name, feature_name, annotators, source_files), self, level)

    def annotations(self, layer_name, feature_name=None, annotators=None, source_files=None):
        layer_name = extend_layer_name(layer_name)
        relevant_annotations = self._filter_annotation_info(annotators, source_files).itertuples(index=False, name=None)

        annotations = []
        for cas, source_file, annotator in relevant_annotations:
            try:
                for annotation in cas.select(layer_name):
                    entry = (annotation, source_file, annotation.begin, annotation.end, annotator)
                    annotations.append(entry)
            except cassis.typesystem.TypeNotFoundError:
                continue

        colnames = ['annotation', 'source_file', 'begin', 'end', 'annotator']

        index = ['source_file', 'begin', 'end', 'annotator']
        annotations = pd.DataFrame(annotations, columns=colnames).set_index(index)

        if feature_name is not None:
            annotations = annotations.applymap(lambda x: x.get(feature_name), na_action='ignore')

        return annotations


class View:
    def __init__(self, annotations, project, level='layer'):
        self.annotations = annotations
        self.level = level
        self.project = project

    @property
    def document_annotator_matrix(self):
        # TODO: handle more elegantly, annotations are lost by dropping duplicates
        return self.annotations.loc[~self.annotations.index.duplicated(), 'annotation'].unstack()

    def counts(self, grouped_by=None):
        annotations = self.annotations
        if grouped_by is not None:
            annotations = self.annotations.groupby(grouped_by)
        return annotations.value_counts()

    def iaa(self, measure='pairwise_kappa', level='nominal'):
        if self.level == 'layer':
            raise ValueError('Inter-Annotator Agreement is only implemented on "annotation" level.')

        matrix = self.document_annotator_matrix

        if measure == 'pairwise_kappa':
            annotators = matrix.columns
            entries = []
            for pair in combinations(annotators, 2):
                annotator_a, annotator_b = pair
                data = matrix[list(pair)].dropna().values.T
                n = data.shape[1]

                score = cohen_kappa_score(data[0], data[1])

                entries.append((annotator_a, annotator_b, n, score))

            return pd.DataFrame(entries, columns=['a', 'b', 'n', 'kappa']).set_index(['a', 'b'])

        if measure == 'krippendorff':
            categories = matrix.stack().unique()
            category_to_index = {category: i for i, category in enumerate(categories)}
            return alpha(matrix.replace(category_to_index).values.T, level_of_measurement=level)
