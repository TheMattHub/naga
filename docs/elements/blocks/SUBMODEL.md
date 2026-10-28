Use SUBMODEL to place a reusable group of blocks on the canvas, such as a workstation or a ward, and to keep a large model readable. The group is defined once, with named entry and exit ports, and used many times with different arguments. It is a tool of the editor and of the model file, not of the run.

## Behaviour and rules

A SUBMODEL names a `definition` and binds its parameters in `arguments`, each a number, an expression or a flag. Before a run, the engine replaces every instance by a copy of the definition's blocks, named `<instance>/<inner>`, and rewires the links through the ports. A submodel therefore costs nothing at run time.

Results and names inside an instance carry that prefix. A sequence step must use the prefixed name of a station inside an instance. A block that runs unflattened stops with an error, which only happens when a layout bypasses the loader.

The `preset` records which catalogue row the arguments came from. It is for provenance only. Editing an argument afterwards is fine, and nothing re-reads the catalogue. The block can have several outgoing links, and each names the exit port it leaves from.
