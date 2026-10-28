These sections organise a model and leave notes for people. `submodels` are reusable definitions that SUBMODEL blocks instantiate. `info` holds the description and author. `annotations` and `background` draw notes, frames and a floor plan on the canvas, and `building` declares the floor plan itself: rooms, wall openings and labels in metres. `view` remembers where the editor was. Only `submodels` changes what a run does.

## Behaviour and rules

A submodel definition holds parameters, inner blocks, links and named ports. Before a run, every instance is replaced by a copy of the definition's blocks, named `<instance>/<inner>`, and the links are rewired through the ports. A definition may be reused with different arguments, and a definition that contains itself is an error.

The engine ignores `annotations`, `background`, `building`, `view` and `info`, apart from showing `info` in the report header. They never change a result. A background image has a position, a scale, an opacity, a lock and a real-world scale. A building's walls are not stored: they are the outlines of its rooms with `walls` true, merged per wall line and cut by its openings. The editor writes `view` on every save.
