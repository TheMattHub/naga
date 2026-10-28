These sections connect a model to data. `data_sources` declare where data lives: a CSV or Excel file, a database, a web address, or a live plant connection. `data_bindings` read a column from a source, reduce it, and write the result into a model parameter. `initial_state` starts a run from a snapshot of a real system instead of an empty one.

## Behaviour and rules

A binding names a source, a column, an optional filter and a `transform`, such as `mean`, `last`, `table` or `fit`. Its `target` is an override path such as `resources.Nurse.capacity`. A binding whose target does not exist is an error. A disabled binding leaves the parameter at its file value. A filter holds comparisons only, never code.

A model never holds an address or a credential. For a live source, `connection` names an entry in the connections file of whoever runs the model. A file path is relative to the model file. Network access, redirects and the size of what is read are limited, and the caller, not the file, decides whether the network may be used.

An `initial_state` places entities in blocks, sets variables, levels and resource states, and sets the clock. Statistics then cover the run from that clock, as after a warm-up of that length. A state that names a block the model does not have is an error. The `schedule_offset` makes a mid-shift start resume the shift.
