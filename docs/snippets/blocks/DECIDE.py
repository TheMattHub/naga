from nagasim import build as nb

m = nb.Model(time_limit=500)
m.add_resource(name="Fast")
m.add_resource(name="Careful")
arrive = m.create(distribution=nb.Exponential(rate=0.3))
split = m.decide(probs=[0.7])                  # 70 percent down link 0, the rest down link 1
fast = m.process(resource_name="Fast", distribution=nb.Constant(value=2))
careful = m.process(resource_name="Careful", distribution=nb.Constant(value=4))
done = m.dispose()
arrive >> split
split >> fast >> done
split >> careful >> done

results = m.run(seed=1)
print(int(results.kpi("resource/Fast/Total Seizes")), "went the fast way,", int(results.kpi("resource/Careful/Total Seizes")), "the careful way")
