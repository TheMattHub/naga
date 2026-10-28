from nagasim import build as nb

m = nb.Model(time_limit=200)
m.add_resource(name="Lathe")
cell = m.definition("Cell")                     # a reusable group: SUB_ENTRY -> blocks -> SUB_EXIT
cell.sub_entry("in") >> cell.process(resource_name="Lathe", distribution=nb.Constant(value=2)) >> cell.sub_exit("out")
m.create(distribution=nb.Constant(value=5)) >> m.submodel(definition="Cell") >> m.dispose()

results = m.run(seed=1)
print(int(results.kpi("resource/Lathe/Total Seizes")), "parts went through the Cell submodel")
