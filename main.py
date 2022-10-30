import math
import torch
import argparse
import numpy as np
from tqdm import tqdm
from utils import load_model
from torch.utils.data import DataLoader
import time
from utils.functions import reconnect
from utils.functions import load_problem
from problems.tsp.tsp_baseline import solve_insertion
import pprint as pp
from concurrent.futures import ProcessPoolExecutor

# mp = torch.multiprocessing.get_context('spawn')


def eval_dataset(dataset_path, opts):
    pp.pprint(vars(opts))
    use_cuda = torch.cuda.is_available() and not opts.no_cuda
    device = torch.device("cuda:0" if use_cuda else "cpu")

    revisers = []
    assert opts.problem_type == 'tsp'
    revision_lens = opts.revision_lens

    for reviser_size in revision_lens:
        reviser_path = f'pretrained/Reviser-stage2/reviser_{reviser_size}/epoch-299.pt'
        
        reviser, _ = load_model(reviser_path, is_local=True)
        revisers.append(reviser)
        
    for reviser in revisers:
        reviser.to(device)
        reviser.eval()
        reviser.set_decode_type(opts.decode_strategy)

    dataset = reviser.problem.make_dataset(filename=dataset_path, num_samples=opts.val_size, offset=0)
    results = _eval_dataset(dataset, opts, device, revisers)

    costs, costs_revised, tours, durations = zip(*results)  # Not really costs since they should be negative
    
    costs = torch.tensor(costs)
    costs_revised = torch.cat(costs_revised, dim=0)
    tours = torch.cat(tours, dim=0)

    print("Average cost: {} +- {}".format(costs.mean(), (2 * torch.std(costs) / math.sqrt(len(costs))).item()))
    print("Average cost_revised: {} +- {}".format(costs_revised.mean().item(), (2 * torch.std(costs_revised) / math.sqrt(len(costs_revised))).item()))
    print("Total duration: {}".format(sum(durations)))
    print("Avg. duration: {}".format(sum(durations) / opts.val_size))

    return costs, tours, durations

def _solve_insertion(args):

    instance, order = args
        
    cost, pi, duration = solve_insertion(
                            directory=None, 
                            name=None, 
                            loc=instance,
                            method='random',
                            order=order
                            )

    return pi

def _eval_dataset(dataset, opts, device, revisers):

    dataloader = DataLoader(dataset, batch_size=opts.eval_batch_size)
    problem = load_problem(opts.problem_type)

    # cost function for partial solution 
    get_cost_func = lambda input, pi: problem.get_costs(input, pi, return_local=True)

    results = []
    for batch in tqdm(dataloader, disable=opts.no_progress_bar):
        # tsp batch shape: (bs, problem size, 2)
        avg_cost = 0
        start = time.time()
        with torch.no_grad():
    
            

            orders = [torch.randperm(opts.problem_size) for i in range(opts.aug_shift)]
            insertion_start = time.time()

            instance = batch[0]

            with ProcessPoolExecutor() as pool:
                futures = [pool.submit(_solve_insertion, index)
                        for index in [
                                (instance, orders[order_id]) for order_id in range(len(orders)) for instance in batch
                            ]
                        ]

                pi_batch = torch.tensor([future.result() for future in futures])
                print(len(pi_batch))

            print('insertion time:', time.time()-insertion_start)

            batch = batch.repeat(opts.aug_shift, 1, 1)

            seed = batch.gather(1, pi_batch.unsqueeze(-1).expand_as(batch))
            seed = seed.to(device)

            cost_ori = (seed[:, 1:] - seed[:, :-1]).norm(p=2, dim=2).sum(1) + (seed[:, 0] - seed[:, -1]).norm(p=2, dim=1)
            cost_ori, _ = cost_ori.reshape(-1, opts.eval_batch_size).min(0) # width, bs
            avg_cost = cost_ori.mean().item()
            print('before revision:', avg_cost)  

            if opts.aug:
                
                # if opts.aug_shift > 1:
                #     seed = torch.cat([torch.roll(seed, i, 1) for i in range(0, opts.aug_shift)], dim=0)
                seed2 = torch.cat((1 - seed[:, :, [0]], seed[:, :, [1]]), dim=2)
                seed3 = torch.cat((seed[:, :, [0]], 1 - seed[:, :, [1]]), dim=2)
                seed4 = torch.cat((1 - seed[:, :, [0]], 1 - seed[:, :, [1]]), dim=2)
                seed = torch.cat((seed, seed2, seed3, seed4), dim=0)
                
                print('\n seed shape after augmentation:', seed.shape)

            # needed shape: (width, graph_size, 2) / (width, graph_size)
            
            tours, costs_revised = reconnect( 
                                        get_cost_func=get_cost_func,
                                        batch=seed,
                                        opts=opts,
                                        revisers=revisers,
                                        )
            # tours shape: problem_size, 2
            # costs: costs before revision
            # costs_revised: costs after revision

        duration = time.time() - start

        if costs_revised is not None:
            results.append((avg_cost, costs_revised, tours, duration))
        else:
            results.append((avg_cost, None, tours, duration))

    return results


if __name__ == "__main__":
 
    parser = argparse.ArgumentParser()
    parser.add_argument("--problem_size", type=int, default=200)
    parser.add_argument("--problem_type", type=str, default='tsp')
    parser.add_argument('--val_size', type=int, default=128,
                        help='Number of instances used for reporting validation performance')
    parser.add_argument('--eval_batch_size', type=int, default=128,
                        help="Batch size to use during (baseline) evaluation")
    parser.add_argument('--revision_lens', nargs='+', default=[100,50,20] ,type=int)
    parser.add_argument('--revision_iters', nargs='+', default=[20,25,10,], type=int)
    parser.add_argument('--shift_lens', nargs='+', default=[5,2,2], type=int)
    parser.add_argument('--decode_strategy', type=str, default='greedy', help='decode strategy of the model')
    parser.add_argument('--no_cuda', action='store_true', help='Disable CUDA')
    parser.add_argument('--no_progress_bar', action='store_true', help='Disable progress bar')
    parser.add_argument('--aug', action='store_true')
    parser.add_argument('--aug_shift', type=int, default=1, help='')
    
    opts = parser.parse_args()

    dataset_path = f'data/tsp/tsp{opts.problem_size}_test.pkl'
    eval_dataset(dataset_path, opts)
