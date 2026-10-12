"""K1 (performance report 2026-10-07): the resource and store log frames are built
from tuples; columns, order and dtypes stay what the dict-built frames had."""
import os

import pandas as pd

from nagasim import api

MODEL = os.path.join(os.path.dirname(api.__file__), "models", "warehouse_library.json")


def test_resource_and_store_log_dtypes_and_columns():
    results = api.run_model(MODEL, seed=0, replications=1)
    resource, store = results["resource_log"], results["store_log"]
    assert len(resource) > 0 and len(store) > 0
    assert list(resource.columns) == ["Run", "Resource", "time", "in_use", "queue_length", "event_type"]
    assert list(store.columns) == ["Run", "Store", "time", "items", "event_type"]
    # The digest hashes CSV text, which cannot see a dtype change: pin them here.
    # A text column has the dtype pandas itself gives text: object in 2, str in 3.
    text = str(pd.Series(["a"]).dtype)
    assert [str(t) for t in resource.dtypes] == ["int64", text, "float64", "int64", "int64", text]
    assert [str(t) for t in store.dtypes] == ["int64", text, "float64", "int64", text]
    assert isinstance(resource, pd.DataFrame)
