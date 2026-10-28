from nagasim import build as nb


def busy_with(correction):
    """A robot re-positions after every pick; the correction time comes from the error."""
    m = nb.Model(time_limit=4000, warmup=0)
    m.add_resource(name="Robot")
    picks = m.create(distribution=nb.Exponential(rate=1 / 3))
    picks >> m.process(resource_name="Robot", distribution=correction) >> m.dispose()
    return m.run(replications=10, seed=1).kpi("resource/Robot/Usage % (snapshot)")


# The size of a two-axis placing error, each axis normal with sd 1, is Rayleigh(sigma=1): mean 1.25.
# Its square is ChiSquare with 2 degrees of freedom: mean 2. A correction that costs a minute per unit
# of error and one that costs a minute per squared unit are 42% and 67% busy.
print(f"cost grows with the error:          {busy_with(nb.Rayleigh(sigma=1)):.0f}% busy")
print(f"cost grows with the squared error:  {busy_with(nb.ChiSquare(df=2)):.0f}% busy")
