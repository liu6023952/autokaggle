import numpy as np
import pandas as pd
import scipy
import itertools
from scipy.stats import pearsonr
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler, PowerTransformer, KBinsDiscretizer
from sklearn.base import TransformerMixin
from sklearn.base import BaseEstimator
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from abc import abstractmethod
import collections
from lightgbm import LGBMClassifier, LGBMRegressor
LEVEL_HIGH = 32


class TabularPreprocessor:
    def __init__(self, verbose=True):
        """
        Initialization function for tabular preprocessor.
        """
        self.verbose = verbose

        self.total_samples = 0
        self.n_first_batch_keys = {}
        self.high_level_cat_keys = []

        self.feature_add_high_cat = 0
        self.feature_add_cat_num = 10
        self.feature_add_cat_cat = 10
        self.order_num_cat_pair = {}

        self.budget = None
        self.data_info = None
        self.pipeline = None

    def fit(self, raw_x, y, time_limit, data_info):
        """
        This function should train the model parameters.

        Args:
            raw_x: a numpy.ndarray instance containing the training data.
            y: training label vector.
            time_limit: remaining time budget.
            data_info: meta-features of the dataset, which is an numpy.ndarray describing the
             feature type of each column in raw_x. The feature type include:
                     'TIME' for temporal feature, 'NUM' for other numerical feature,
                     and 'CAT' for categorical feature.
        """
        self.budget = time_limit
        # Extract or read data info
        self.data_info = data_info if data_info is not None else self.extract_data_info(raw_x)

        data = TabularData(raw_x, self.data_info, self.verbose)

        self.pipeline = Pipeline([
            ('imputer', Imputation(selected_type='ALL', operation='upd')),
            ('cat_num_encoder', CatNumEncoder(selected_type1='CAT', selected_type2='NUM')),
            ('cat_num_encoder', CatCatEncoder(selected_type1='CAT', selected_type2='CAT')),
            ('target_encoder', TargetEncoder(selected_type='CAT', operation='add')),
            ('count_encoder', CatCount(selected_type='CAT', operation='add')),
            ('label_encoder', CatEncoder(selected_type='CAT', operation='add')),
            ('boxcox', BoxCox(selected_type='NUM', operation='upd')),
            ('log_square', LogTransform(selected_type='NUM', operation='upd')),
            ('scaler', TabScaler(selected_type='NUM', operation='upd')),
            ('binning', Binning(selected_type='NUM', operation='add')),
            ('pca', TabPCA(selected_type='NUM', operation='add')),
            ('time_diff', TimeDiff(selected_type='TIME', operation='upd')),
            ('time_offset', TimeOffset(selected_type='TIME', operation='upd')),
            ('filter', FilterConstant(selected_type='ALL', operation='del')),
            ('pearson_corr', FeatureFilter(selected_type='ALL', operation='del')),
            ('lgbm_feat_selection', FeatureImportance(selected_type='ALL', operation='del')),
        ])
        self.pipeline.fit(data, y)

        return self

    def transform(self, raw_x, time_limit=None):
        """
        This function should train the model parameters.

        Args:
            raw_x: a numpy.ndarray instance containing the training/testing data.
            time_limit: remaining time budget.
        Both inputs X and y are numpy arrays.
        If fit is called multiple times on incremental data (train, test1, test2, etc.)
        you should warm-start your training from the pre-trained model. Past data will
        NOT be available for re-training.
        """
        # Get Meta-Feature
        if time_limit is None:
            if self.budget is None:
                time_limit = 24 * 60 * 60
                self.budget = time_limit
        else:
            self.budget = time_limit

        data = TabularData(raw_x, self.data_info, self.verbose)
        return self.pipeline.transform(data).X.values

    @staticmethod
    def extract_data_info(raw_x):
        """
        This function extracts the data info automatically based on the type of each feature in raw_x.

        Args:
            raw_x: a numpy.ndarray instance containing the training data.
        """
        data_info = []
        row_num, col_num = raw_x.shape
        for col_idx in range(col_num):
            try:
                raw_x[:, col_idx].astype(np.float)
                data_info.append('NUM')
            except:
                data_info.append('CAT')
        return np.array(data_info)


class TabularData:
    cat_col = None
    num_col = None
    time_col = None
    n_cat, n_time, n_num = 0, 0, 0
    cat_cardinality = None
    generated_features = None
    feature_options = None
    num_info = None

    def __init__(self, raw_x, data_info, verbose=True):
        self.verbose = verbose
        self.data_info = {str(i): data_info[i] for i in range(len(data_info))}
        self.total_samples = raw_x.shape[0]
        self.refresh_col_types()

        # Convert sparse to dense if needed
        raw_x = raw_x.toarray() if type(raw_x) == scipy.sparse.csr.csr_matrix else raw_x

        # To pandas Dataframe
        if type(raw_x) != pd.DataFrame:
            raw_x = pd.DataFrame(raw_x, columns=[str(i) for i in range(raw_x.shape[1])])

        self.X = raw_x
        self.cat_cardinality = {}
        self.update_cat_cardinality()

        if self.verbose:
            print('DATA_INFO: {}'.format(self.data_info))
            print('#TIME features: {}'.format(self.n_time))
            print('#NUM features: {}'.format(self.n_num))
            print('#CAT features: {}'.format(self.n_cat))

    def update_type(self, columns, new_type):
        if not new_type:
            return
        for c in columns:
            self.data_info[c] = new_type

    def delete_type(self, columns):
        for c in columns:
            _ = self.data_info.pop(c, 0)

    def update(self, operation, columns, x_tr, new_type=None):
        if operation == 'upd':
            if x_tr is not None:
                self.X[columns] = x_tr
            self.update_type(columns, new_type)
        elif operation == 'add':
            if x_tr is not None:
                self.X = pd.concat([self.X, x_tr], axis=1)
                self.update_type(x_tr.columns, new_type)
        elif operation == 'del':
            if len(columns) != 0:
                self.X.drop(columns, inplace=True)
                self.delete_type(columns)
        else:
            print("invalid operation")
        self.refresh_col_types()

    def refresh_col_types(self):
        self.cat_col = [k for k, v in self.data_info.items() if v == 'CAT']
        self.num_col = [k for k, v in self.data_info.items() if v == 'NUM']
        self.time_col = [k for k, v in self.data_info.items() if v == 'TIME']
        self.n_time = len(self.time_col)
        self.n_num = len(self.num_col)
        self.n_cat = len(self.cat_col)

    def update_cat_cardinality(self):
        for c in self.cat_col:
            self.cat_cardinality[c] = len(set(self.X[c]))

    def select_columns(self, data_type):
        self.refresh_col_types()
        if data_type == 'CAT':
            return self.cat_col
        elif data_type == 'TIME':
            return self.time_col
        elif data_type == 'NUM':
            return self.num_col
        elif data_type == 'ALL':
            return list(self.data_info.keys())
        else:
            print('invalid Type')
            return []


class Primitive(BaseEstimator, TransformerMixin):
    selected = None
    drop_columns = None

    def __init__(self, selected_type=None, operation='upd', **kwargs):
        self.selected_type = selected_type
        self.operation = operation

    def fit(self, data, y=None):
        self.selected = data.select_columns(self.selected_type)
        if not self.selected:
            return self
        return self._fit(data, y)

    def transform(self, data, y=None):
        if not self.selected:
            return data
        return self._transform(data, y)

    @abstractmethod
    def _fit(self, data, y=None):
        pass

    @abstractmethod
    def _transform(self, data, y=None):
        pass


class PrimitiveHigherOrder(Primitive):
    def __init__(self, operation='upd', selected_type1=None, selected_type2=None, **kwargs):
        self.selected_type1 = selected_type1
        self.selected_type2 = selected_type2
        self.operation = operation
        self.options = kwargs


class TabScaler(Primitive):
    scaler = None

    def _fit(self, data, y=None):
        self.scaler = StandardScaler()
        self.scaler.fit(data.X[self.selected], y)
        return self

    def _transform(self, data, y=None):
        x_tr = self.scaler.transform(data.X[self.selected])
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class BoxCox(Primitive):
    transformer = None

    def _fit(self, data, y=None):
        self.transformer = PowerTransformer()
        self.transformer.fit(data.X[self.selected], y)
        return self

    def _transform(self, data, y=None):
        x_tr = self.transformer.transform(data.X[self.selected])
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class Binning(Primitive):
    binner = None

    def __init__(self, selected_type=None, operation='upd', strategy='quantile', encoding='ordinal'):
        super().__init__(selected_type, operation)
        self.strategy = strategy
        self.encoding = encoding

    def _fit(self, data, y=None):
        self.binner = KBinsDiscretizer(strategy=self.strategy, encode=self.encoding)
        self.binner.fit(data.X[self.selected], y)
        return self

    def _transform(self, data, y=None):
        x_tr = self.binner.transform(data.X[self.selected])
        # TODO: decide if cat or num new type
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class CatEncoder(Primitive):
    cat_to_int_label = None

    def _fit(self, data, y=None):
        X = data.X
        self.cat_to_int_label = {}
        for col_index in self.selected:
            self.cat_to_int_label[col_index] = self.cat_to_int_label.get(col_index, {})
            for row_index in range(len(X)):
                key = str(X[row_index, col_index])
                if key not in self.cat_to_int_label[col_index]:
                    self.cat_to_int_label[col_index][key] = len(self.cat_to_int_label[col_index])
        return self

    def _transform(self, data, y=None):
        X = data.X
        for col_index in self.selected:
            for row_index in range(len(X)):
                key = str(X[row_index, col_index])
                X[row_index, col_index] = self.cat_to_int_label[col_index].get(key, np.nan)
        return data


class TargetEncoder(Primitive):
    target_encoding_map = None

    @staticmethod
    def calc_smooth_mean(df, by, on, alpha=5):
        # Compute the global mean
        mean = df[on].mean()

        # Compute the number of values and the mean of each group
        agg = df.groupby(by)[on].agg(['count', 'mean'])
        counts = agg['count']
        means = agg['mean']

        # Compute the "smoothed" means
        smooth = (counts * means + alpha * mean) / (counts + alpha)
        return smooth

    def _fit(self, data, y=None):
        X = data.X
        self.target_encoding_map = {}
        X['target'] = y
        for col in self.selected:
            self.target_encoding_map[col] = self.calc_smooth_mean(X, col, 'target', alpha=5)
        X.drop('target', axis=1, inplace=True)
        return self

    def _transform(self, data, y=None):
        x_tr = pd.DataFrame()
        for col in self.selected:
            x_tr[col] = data.X[col].map(self.target_encoding_map[col])
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class CatCatEncoder(PrimitiveHigherOrder):
    @staticmethod
    def cat_cat_count(df, col1, col2, strategy='count'):
        if strategy == 'count':
            mapping = df.groupby([col1])[col2].count()
        elif strategy == 'nunique':
            mapping = df.groupby([col1])[col2].nunique()
        else:
            mapping = df.groupby([col1])[col2].count() // df.groupby([col1])[col2].nunique()
        return mapping

    def _fit(self, data, y=None):
        self.cat_cat_map = {}
        self.strategy = self.options.get('strategy', 'count')
        for col1, col2 in itertools.combinations(self.selected, 2):
            self.cat_cat_map[col1 + '_cross_' + col2] = self.cat_cat_count(data.X, col1, col2, self.strategy)
        return self

    def _transform(self, data, y=None):
        x_tr = pd.DataFrame()
        for col1, col2 in itertools.combinations(self.selected, 2):
            if col1 + '_cross_' + col2 in self.cat_cat_map:
                x_tr[col1 + '_cross_' + col2] = data.X[col1].map(self.cat_cat_map[col1 + '_cross_' + col2])
        # TODO: decide new_type
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class CatNumEncoder(PrimitiveHigherOrder):
    def __init__(self, selected_type=None, selected_num=[], operation='add', strategy='mean'):
        super().__init__(selected_type, operation)
        self.selected_num = selected_num
        self.strategy = strategy
        self.cat_num_map = {}

    @staticmethod
    def cat_num_interaction(df, col1, col2, method='mean'):
        if method == 'mean':
            mapping = df.groupby([col1])[col2].mean()
        elif method == 'std':
            mapping = df.groupby([col1])[col2].std()
        elif method == 'max':
            mapping = df.groupby([col1])[col2].max()
        elif method == 'min':
            mapping = df.groupby([col1])[col2].min()
        else:
            mapping = df.groupby([col1])[col2].mean()

        return mapping

    def _fit(self, data, y=None):
        self.cat_num_map = {}
        self.strategy = self.options.get('strategy', 'mean')
        for col1 in self.selected:
            for col2 in self.selected_num:
                self.cat_num_map[col1 + '_cross_' + col2] = self.cat_num_interaction(data.X, col1, col2, self.strategy)
        return self

    def _transform(self, data, y=None):
        x_tr = pd.DataFrame()
        for col1 in self.selected:
            for col2 in self.selected_num:
                if col1 + '_cross_' + col2 in self.cat_num_map:
                    x_tr[col1 + '_cross_' + col2] = data.X[col1].map(self.cat_num_map[col1 + '_cross_' + col2])
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class CatBinEncoder(PrimitiveHigherOrder):
    @staticmethod
    def cat_bin_interaction(df, col1, col2, strategy='percent_true'):
        if strategy == 'percent_true':
            mapping = df.groupby([col1])[col2].mean()
        elif strategy == 'count':
            mapping = df.groupby([col1])[col2].count()
        else:
            mapping = df.groupby([col1])[col2].mean()
        return mapping

    def _fit(self, data, y=None):
        self.cat_bin_map = {}
        self.strategy = self.options.get('strategy', 'percent_true')
        for col1 in self.selected:
            for col2 in self.selected_bin:
                self.cat_bin_map[col1 + '_cross_' + col2] = self.cat_bin_interaction(data.X, col1, col2, self.strategy)
        return self

    def _transform(self, data, y=None):
        x_tr = pd.DataFrame()
        for col1 in self.selected:
            for col2 in self.selected_bin:
                if col1 + '_cross_' + col2 in self.cat_bin_map:
                    x_tr[col1 + '_cross_' + col2] = data.X[col1].map(self.cat_bin_map[col1 + '_cross_' + col2])
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class FilterConstant(Primitive):
    drop_columns = None

    def _fit(self, data, y=None):
        X = data.X
        self.drop_columns = X.columns[(X.max(axis=0) - X.min(axis=0) == 0)].tolist()
        return self

    def _transform(self, data, y=None):
        data.update(self.operation, self.drop_columns, None, new_type=None)
        return data


class TimeDiff(Primitive):

    def _fit(self, data, y=None):
        return self

    def _transform(self, data, y=None):
        x_tr = pd.DataFrame()
        for a, b in itertools.combinations(self.selected, 2):
            x_tr[a + '-' + b] = data.X[a] - data.X[b]
        data.update(self.operation, self.selected, x_tr, new_type='TIME')
        return data


class TimeOffset(Primitive):
    start_time = None

    def _fit(self, data, y=None):
        self.start_time = data.X[self.selected].min(axis=0)
        return self

    def _transform(self, data, y=None):
        x_tr = pd.DataFrame()
        x_tr[self.selected] = data.X[self.selected] - self.start_time
        data.update(self.operation, self.selected, x_tr, new_type='TIME')
        return data


class TabPCA(Primitive):
    pca = None

    def _fit(self, data, y=None):
        self.pca = PCA(n_components=0.99, svd_solver='full')
        self.pca.fit(data.X[self.selected])
        return self

    def _transform(self, data, y=None):
        x_pca = self.pca.transform(data.X[self.selected])
        x_pca = pd.DataFrame(x_pca, columns=['pca_' + str(i) for i in range(x_pca.shape[1])])
        data.update(self.operation, self.selected, x_pca, new_type='NUM')
        return data


class CatCount(Primitive):
    count_dict = None

    def _fit(self, data, y=None):
        self.count_dict = {}
        for col in self.selected:
            self.count_dict[col] = collections.Counter(data.X[col])
        return self

    def _transform(self, data, y=None):
        x_tr = pd.DataFrame()
        for col in self.selected:
            x_tr[col] = data.X[col].apply(lambda key: self.count_dict[col][key])
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class LogTransform(Primitive):
    name_key = 'log_'

    def _fit(self, data, y=None):
        return self

    def _transform(self, data, y=None):
        x_tr = pd.DataFrame()
        for col in self.selected:
            x_tr[self.name_key + col] = np.square(np.log(1 + data.X[col]))
        data.update(self.operation, self.selected, x_tr, new_type='NUM')
        return data


class Imputation(Primitive):
    impute_dict = None

    def _fit(self, data, y=None):
        self.impute_dict = {}
        for col in self.selected:
            value_counts = data.X[col].value_counts()
            self.impute_dict[col] = value_counts.idxmax() if not value_counts.empty else 0
        return self

    def _transform(self, data, y=None):
        for col in self.selected:
            data.X[col].fillna(self.impute_dict[col])
        data.update(self.operation, self.selected, None, new_type='NUM')
        return data


class FeatureFilter(Primitive):
    def __init__(self, selected_type=None, operation='del', threshold=0.001):
        super().__init__(selected_type, operation)
        self.threshold = threshold
        self.drop_columns = []

    def _fit(self, data, y=None):
        for col in self.selected:
            mu = abs(pearsonr(data.X[col], y)[0])
            if np.isnan(mu):
                mu = 0
            if mu < self.threshold:
                self.drop_columns.append(col)
        return self

    def _transform(self, data, y=None):
        data.update(self.operation, self.drop_columns, None, new_type=None)
        return data


class FeatureImportance(Primitive):
    def __init__(self, selected_type=None, operation='del', threshold=0.001, task_type='classification'):
        super().__init__(selected_type, operation)
        self.threshold = threshold
        self.drop_columns = []
        self.task_type = task_type

    def _fit(self, data, y=None):
        if self.task_type == 'classification':
            n_classes = len(set(y))
            if n_classes == 2:
                estimator = LGBMClassifier(silent=False,
                                           verbose=-1,
                                           n_jobs=1,
                                           objective='binary')
            else:
                estimator = LGBMClassifier(silent=False,
                                           verbose=-1,
                                           n_jobs=1,
                                           num_class=n_classes,
                                           objective='multiclass')
        else:
            # self.task_type == 'regression'
            estimator = LGBMRegressor(silent=False,
                                      verbose=-1,
                                      n_jobs=1,
                                      objective='regression')
        estimator.fit(data.X, y)
        feature_importance = estimator.feature_importances_
        feature_importance = feature_importance/feature_importance.mean()
        self.drop_columns = data.X.columns[np.where(feature_importance < self.threshold)[0]]
        return self

    def _transform(self, data, y=None):
        data.update(self.operation, self.drop_columns, None, new_type=None)
        return data


if __name__ == "__main__":
    ntime, nnum, ncat = 4, 10, 8
    nsample = 1000
    x_num = np.random.random([nsample, nnum])
    x_time = np.random.random([nsample, ntime])
    x_cat = np.random.randint(0, 10, [nsample, ncat])

    x_all = np.concatenate([x_num, x_time, x_cat], axis=1)
    x_train = x_all[:int(nsample * 0.8), :]
    x_test = x_all[int(nsample * 0.8):, :]

    y_all = np.random.randint(0, 2, nsample)
    y_train = y_all[:int(nsample * 0.8)]
    y_test = y_all[int(nsample * 0.8):]

    datainfo = np.array(['TIME'] * ntime + ['NUM'] * nnum + ['CAT'] * ncat)
    print(x_train[:4, 20])
    prep = TabularPreprocessor()
    prep.fit(x_train, y_train, 24*60*60, datainfo)
    x_new = prep.transform(x_train)

    print("-----")
    print(x_new[:4, 2])

