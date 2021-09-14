import cassis
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from itertools import combinations
from sklearn.metrics import cohen_kappa_score
from krippendorff import alpha
from utils import extend_layer_name, annotation_info_from_xmi_zip, source_files_from_xmi_zip, get_dtype, \
    confusion_matrix, heatmap, percentage_agreement, SENTENCE_TYPE_NAME
from utils import gamma_agreement, construct_feature_path
from typing import Union, Sequence, List


class Project:
    @classmethod
    def from_zipped_xmi(cls, project_path):
        """
        Loads an Inception project exported to XMI format, located at the given path.

        Args:
            project_path (str): A string representing the path to the exported project.
        """
        annotations = annotation_info_from_xmi_zip(project_path)
        source_files = source_files_from_xmi_zip(project_path)
        return cls(annotations, source_files, project_path, 'xmi')

    def __init__(self, annotations, source_files, project_path, export_format):
        self._annotation_info = pd.DataFrame(annotations, columns=['cas', 'source_file', 'annotator'])
        self.path = project_path
        self.export_format = export_format
        self.layer_feature_separator = '>'
        self.all_source_file_names = source_files

    @property
    def typesystem(self):
        """Returns the Typesystem used by the CAS Objects in the Project."""
        return self._annotation_info.loc[0, 'cas'].typesystem

    @property
    def layers(self) -> List[str]:
        """Returns a list of all layer names in the project."""
        return [t.name for t in self.typesystem.get_types()]

    @property
    def custom_layers(self) -> List[str]:
        """Returns a list of all custom layer names in the project."""
        return [l for l in self.layers if l.startswith('webanno.custom')]

    @property
    def source_file_names(self) -> List[str]:
        """Returns a list of all source file names that have at least a single annotation attached."""
        return self._unique_entries('source_file')

    @property
    def empty_source_file_names(self) -> List[str]:
        """Returns a list of all source file names that are not annotated."""
        annotated = np.asarray(self.source_file_names)
        all = np.asarray(self.all_source_file_names)
        mask = np.isin(all, annotated, invert=True)
        return all[mask].tolist()

    @property
    def annotators(self) -> List[str]:
        """Returns a list of all annotator names with at least a single annotation in the project."""
        return self._unique_entries('annotator')

    @property
    def cas_objects(self) -> List[cassis.Cas]:
        """Returns a list with all CAS Objects in the project."""
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

    def feature_path(self, layer: str, feature:str):
        """Returns a path from the given layer and feature for passing to the Project.view method."""
        return construct_feature_path(layer, feature, self.layer_feature_separator)

    def features(self, layer_name: str, filter_empty=False) -> List[str]:
        """Returns a list of all feature names for the given layer name.

        Args:
              layer_name: The layer from which to pull feature names.
              filter_empty: If set to true, features without any annotations are filtered out. Note that
        """
        layer_name = extend_layer_name(layer_name)
        no_features = ['begin', 'end', 'sofa']
        feature_names = (f.name for f in self.typesystem.get_type(layer_name).all_features if f.name not in no_features)

        if filter_empty:
            feature_names = (name for name in feature_names if any(self.select(f'{layer_name}>{name}').annotations))

        return list(feature_names)

    def select(self,
               annotation: str,
               annotators: Union[str, List[str]] = None,
               source_files: Union[str, List[str]] = None):
        """
        Returns a View object, based on the specified selection parameters.

        Args:
            annotation: String specifying the annotation to select (combination of layer and feature name). Feature
                names must be separated from the layer names by the project's separator. If no feature is specified, the
                text covered by the annotation is used as the layer's feature.
            annotators: List of annotators to be included. A single annotator can be selected by passing a string. If
                None is provided, all annotators are included in the view.
            source_files: List of source files to be included. A single source file can be selected by passing a string.
                If None is provided, all annotators are included in the view.
        """
        layer_name, feature_name = self._layer_feature_split(annotation)
        layer_name = extend_layer_name(layer_name)
        info = self._filter_annotation_info(annotators, source_files)
        annotations = self._annotations(info, layer_name, feature_name)
        return View(annotations, self, layer_name, feature_name)

    def _layer_feature_split(self, layer_feature_string):
        split = layer_feature_string.rsplit(self.layer_feature_separator, 1)
        if len(split) == 2:
            return split
        else:
            return split[0], None

    def _annotations(self, annotation_info, layer_name, feature_name):
        entries = []
        for cas, source_file, annotator in annotation_info.itertuples(index=False, name=None):
            for sentence in cas.select(SENTENCE_TYPE_NAME):
                try:
                    for annotation in cas.select_covered(layer_name, sentence):
                        sentence_id = f'{source_file}_{sentence.begin}-{sentence.end}'
                        entry = (annotation,
                                 annotation.get_covered_text(),
                                 source_file,
                                 sentence_id,
                                 annotation.begin,
                                 annotation.end,
                                 annotator)
                        entries.append(entry)
                except cassis.typesystem.TypeNotFoundError:
                    continue
        columns = ['_annotation', 'text', 'source_file', 'sentence', 'begin', 'end', 'annotator']
        index = ['source_file', 'sentence', 'begin', 'end', 'annotator']
        annotations = pd.DataFrame(entries, columns=columns).set_index(index)

        if feature_name is not None:
            annotations['annotation'] = annotations['_annotation'].map(lambda x: x.get(feature_name), na_action='ignore')

            # convert java boolean strings into booleans
            dtype = get_dtype(self.typesystem, layer_name, feature_name)
            if dtype == 'bool':
                annotations['annotation'] = annotations['annotation'].map(lambda x: x.startswith('t'), na_action='ignore')

        else:
            annotations['annotation'] = annotations['text']

        # map None value to 'None' String
        # TODO check side effects of this
        annotations['annotation'].replace(to_replace=[None], value='None', inplace=True)

        return annotations


class View:
    _pairwise_iaa_measures = {
        'kappa': cohen_kappa_score,
        'percentage': percentage_agreement
    }

    _aggregate_iaa_measures = {
        'krippendorff': alpha,
        'gamma': gamma_agreement
    }

    def __init__(self, annotations, project, layer_name, feature_name=None):
        self._annotation_dataframe = annotations
        self.project = project
        self.layer_name = layer_name
        self.feature_name = feature_name

    @property
    def level(self):
        """Returns whether the view represents a Layer or a specific Feature of a Layer"""
        return 'layer' if self.feature_name is None else 'feature'

    @property
    def annotations(self) -> pd.Series:
        """Returns a Series of all annotations in the view."""
        return self._annotation_dataframe['annotation']

    @property
    def texts(self) -> pd.Series:
        """Returns a Series of all texts covered by annotations in the view."""
        return self._annotation_dataframe['text']

    @property
    def annotators(self) -> List[str]:
        """Returns a list containing all annotators in the view."""
        return self.data_frame['annotator'].unique().tolist()

    @property
    def labels(self) -> List[any]:
        """Returns a list of all unique annotation values"""
        labels = self.annotations.unique()
        labels.sort()
        return labels.tolist()

    @property
    def data_frame(self) -> pd.DataFrame:
        """Returns a DataFrame with annotation information."""
        return self._annotation_dataframe.reset_index()

    @property
    def document_annotator_matrix(self) -> pd.DataFrame:
        """Returns a Dataframe with document names as indices and annotator names als """
        # TODO: handle more elegantly, annotations are lost by dropping duplicates
        return self._annotation_dataframe.loc[~self._annotation_dataframe.index.duplicated(), 'annotation'].unstack()

    def value_counts(self, grouped_by: Union[str, Sequence[str]] = None) -> pd.Series:
        """
        Returns a Series containing value counts of the feature included in the view.

        Args:
            grouped_by: Name of the variable to group the counts by, either "annotator", "source_file" or a list
                containing both. If a list is given, the order of the variables determines the nesting order.
        """
        annotations = self._annotation_dataframe
        if grouped_by is not None:
            annotations = self._annotation_dataframe.groupby(grouped_by)
        return annotations['annotation'].value_counts()

    def count(self, grouped_by=None, include_empty_files=False):
        """
        Returns a Series containing number of annotations included in the view.

        Args:
            grouped_by: Name of the variable to group the counts by, either "annotator", "source_file" or a list
                containing both. If a list is given, the order of the variables determines the nesting order.
            include_empty_files: If True, empty files will be included in the output. Ignored when grouped_by is None.
        """
        annotations = self._annotation_dataframe.copy()\
            .drop(columns=['_annotation', 'text'])\
            .droplevel('sentence')

        if include_empty_files and grouped_by:
            annotators = annotations.reset_index()['annotator'].unique()
            unannotated_files = self.project.all_source_file_names
            dummy_entries = []

            for annotator in annotators:
                for file in unannotated_files:
                    dummy_entries.append((file, -1, -1, annotator, 'NA'))

            annotations = pd.DataFrame(dummy_entries, columns=annotations.reset_index().columns)\
                .set_index(annotations.index.names)\
                .append(annotations)

        if grouped_by is not None:
            annotations = annotations.groupby(grouped_by)

        counts = annotations['annotation'].count()

        if include_empty_files and grouped_by:
            if isinstance(grouped_by, str) or len(grouped_by) == 1:
                overcount = len(annotators)  # 1 grouping level
            else:
                overcount = 1  # 2 grouping levels
            counts -= overcount

        return counts

    def confusion_matrices(self) -> pd.Series:
        """Returns a Series containing pairwise confusion matrices for every combination of annotators in the View."""
        if len(self.annotators) < 2:
            return pd.Series(dtype='object')

        M = self.document_annotator_matrix
        pairs = [list(pair) for pair in combinations(self.annotators, 2)]
        labels = self.labels

        entries = []
        for pair in pairs:
            a, b = pair
            cm = confusion_matrix(M, pair, labels)
            cm_df = pd.DataFrame(cm, index=labels, columns=labels).rename_axis(index=a, columns=b)
            entries.append((a, b, cm_df))

        index = ['a', 'b']
        name = 'confusion_matrix'
        return pd.DataFrame(entries, columns=[*index, name]).set_index(index)[name]

    def confusion_matrix_plots(self):
        """Returns a Series of confusion matrix plots for every combination of annotators in the view."""
        return self.confusion_matrices().apply(heatmap)

    def pairwise_kappa(self, measure='kappa') -> pd.Series:
        """
        Returns a Series of pairwise inter-annotator agreement statistics for all annotators.

        Args:
            measure: Name of the measure to use, either 'kappa' (default), 'percentage'.
        """
        if measure in self._pairwise_iaa_measures:
            agreement_fn = self._pairwise_iaa_measures.get(measure)
        else:
            raise ValueError(f'"measure" must be one of {self._pairwise_iaa_measures.keys()}, but was "{measure}"!')

        annotators = self.annotators
        M = self.document_annotator_matrix

        entries = []
        for pair in combinations(annotators, 2):
            a, b = pair
            data = M[list(pair)].dropna()
            n = len(data)
            score = agreement_fn(data[a], data[b])
            entries.append((a, b, n, score))

        return pd.DataFrame(entries, columns=['a', 'b', 'n', measure]).set_index(['a', 'b'])

    def iaa(self, measure='krippendorff', level='nominal') -> float:
        """
        Returns inter-annotator agreement statistics for features in the view.

        Args:
            measure: Name of the measure to use, either 'krippendorff' for Krippendorff's Alpha (default) or 'kappa',
                for average pairwise Cohen's Kappa score.
            level: Variable scale to use, when calculating Krippendorff's Alpha. Valid values are 'nominal' (default),
                'ordinal' and 'interval'.
        """
        M = self.document_annotator_matrix

        if measure in self._aggregate_iaa_measures:
            agreement_fn = self._aggregate_iaa_measures[measure]

            if measure == 'krippendorff':
                if level == 'nominal':
                    category_to_index = {category: i for i, category in enumerate(self.labels)}
                    M.replace(category_to_index, inplace=True)
                return agreement_fn(M.values.T, level_of_measurement=level)

            if measure == 'gamma':
                M = self._annotation_dataframe.reset_index()
                return agreement_fn(M)

        if measure in self._pairwise_iaa_measures:
            scores = self.iaa_pairwise(measure)
            return np.average(scores[measure], weights=scores['n'])

        possible_measures = list(self._aggregate_iaa_measures.keys()) + list(self._pairwise_iaa_measures.keys())
        raise ValueError(f'"measure" must be one of {possible_measures}, but was "{measure}"!')

    def progress_chart(self, include_empty_files=True, normalize=False):
        counts = self.count(['source_file', 'annotator'], include_empty_files=include_empty_files).unstack()

        if normalize:
            counts = counts.div(counts.max(axis=1), axis=0)  # normalize by files

        annotators = counts.columns
        files = counts.index
        fig = go.Figure(data=go.Heatmap(
            z=counts,
            x=annotators,
            y=files,
            colorscale='Blues'))

        fig.update_layout(
            yaxis_nticks=0)

        return fig
