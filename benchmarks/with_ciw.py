"""The models in Ciw: the network is declared, and the time in system is read from the
records Ciw keeps of every visit."""
import ciw

from models import ARRIVAL_RATE


def run(stations, run_length, warmup, replications):
    """Mean time in system of each replication (customers who arrived after the warm-up)."""
    network = ciw.create_network(
        arrival_distributions=[ciw.dists.Exponential(ARRIVAL_RATE)] + [None] * (len(stations) - 1),
        service_distributions=[ciw.dists.Exponential(rate) for _count, rate, _to in stations],
        number_of_servers=[count for count, _rate, _to in stations],
        routing=[[to.get(j, 0.0) for j in range(len(stations))] for _count, _rate, to in stations])
    means = []
    for seed in range(replications):
        ciw.seed(seed)
        simulation = ciw.Simulation(network)
        simulation.simulate_until_max_time(run_length)
        records = simulation.get_all_records()
        arrived = {}
        for record in records:              # one record per visit: the first one is the arrival
            arrived[record.id_number] = min(record.arrival_date,
                                            arrived.get(record.id_number, record.arrival_date))
        times = [record.exit_date - arrived[record.id_number] for record in records
                 if record.destination == -1 and arrived[record.id_number] >= warmup]
        means.append(sum(times) / len(times))
    return means
