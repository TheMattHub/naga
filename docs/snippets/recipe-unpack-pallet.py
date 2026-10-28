from nagasim import build as nb

# Sixty parts are packed five to a pallet, the pallet travels for 15 minutes, and SEPARATE turns it
# back into its five parts, which then pass one unpacker a minute each. Parts wait in the pallet
# while it fills, so their time in the model is longer than the 15-minute trip.
m = nb.Model(time_limit=500, warmup=0)
m.add_resource(name="Unpacker")
parts = m.create(distribution=nb.Constant(value=2), max_arrivals=60)
pack = m.batch(batch_size=5, batched_entity_type="PALLET")
trip = m.delay(distribution=nb.Constant(value=15))
unpack = m.separate()
parts >> pack >> trip >> unpack >> m.process(resource_name="Unpacker", distribution=nb.Constant(value=1)) >> m.dispose()

summary = m.run(seed=1)["entity_summary"]
print(summary[["Entity Type", "Total Created", "Total Completed", "Avg Time"]].round(1).to_string(index=False))
