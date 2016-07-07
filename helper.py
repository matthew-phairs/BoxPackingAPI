from fulfillment_api.errors import BoxError
from fulfillment_api.shipments.package_helper import compute_package_weight
from fulfillment_api.authentication.shipping_box import ShippingBox
from fulfillment_api.constants import usps_shipping

from .packing_algorithm import (packing_algorithm, does_it_fit, SkuTuple,
                                volume, pack_boxes)

from itertools import izip
import math
from sqlalchemy import or_

def select_useable_boxes(session, min_box_dimensions, team,
                         flat_rate_okay=False):
    '''
    queries the database for boxes that match criteria team, flat_rate, and size

    Args:
        session (sqlalchemy.orm.session.Session)
        min_box_dimensions (List[int, int, int])
        team (Team),
        flat_rate_okay (Boolean)

    Returns:
        List[Dict[{'box': ShippingBox,
                   'dimensions': List[int, int, int]}]]: a list of useable
            shipping boxes and their dimensions
    '''
    useable_boxes = []
    shipping_query = session.query(ShippingBox).filter(
        or_(ShippingBox.width_cm >= min_box_dimensions[2],
            ShippingBox.height_cm >= min_box_dimensions[2],
            ShippingBox.length_cm >= min_box_dimensions[2]),
        ShippingBox.is_available.is_(True),
        or_(ShippingBox.team_id == team.id,
            ShippingBox.team_id.is_(None)))
    if not flat_rate_okay:
        # only select boxes that are not flat or regional rate
        shipping_query = shipping_query.filter(
            ~ShippingBox.description.in_(usps_shipping.USPS_BOXES))

    boxes = shipping_query.all()

    for box in boxes:
        box_dims = sorted([box.width_cm, box.height_cm, box.length_cm])
        # make sure we only look at boxes where every sku will fit
        if does_it_fit(min_box_dimensions, box_dims):
            useable_boxes.append({'box': box, 'dimensions': box_dims})
    # sort boxes by volume, smallest first and return
    return sorted(useable_boxes, key=lambda box: box['box'].total_cubic_cm)


def api_packing_algorithm(boxes_info, skus_info, options):
    boxes = []
    total_weight = 0
    skus = []
    min_box_dimensions = [None, None, None]
    for sku in skus_info:
        dimensions = sorted(float(sku['width']), float(sku['height']),
                            float(sku['length']))
        skus.append(SkuTuple(sku['sku_number'], dimensions))
        weight_units = sku['weight_units']
        total_weight += convert_mass_units(float(sku['weight']), weight_units,
                                           to_units='grams')
        min_box_dimensions = [max(a, b) for a, b in izip(dimensions,
                                                         min_box_dimensions)]
    max_weight = options.get('max_weight') or 31710
    for box in boxes_info:
        dimensions = sorted(float(box['width']), float(box['height']),
                            float(box['length']))
        if does_it_fit(min_box_dimensions, dimensions):
            boxes.append({
                'box': box['name'],
                'dimensions': dimensions
            })

    min_boxes_by_weight = math.ceil(total_weight / max_weight)
    # sort boxes by volume
    boxes = sorted(boxes, key=lambda box: volume(box['dimensions']))
    # send everything through the packing algorithm
    box_dictionary = packing_algorithm(skus, boxes, min_boxes_by_weight)
    # only return the package, because these boxes don't have description so
    # flat_rate boxes won't be a thing - at least for now
    return box_dictionary['package']


def shotput_db_packing_algorithm(session, team, qty_per_sku,
                                 flat_rate_okay=False, zone=None,
                                 preferred_max_weight=None):
    '''
    from skus provided, and boxes available, pack boxes with skus

    - returns a dictionary of boxes with an 2D array of skus packed
        in each parcel

    Args:
        session (sqlalchemy.orm.session.Session)
        team (Team)
        qty_per_sku (Dict[str, Dict[{
            'sku': SimpleSku,
            'quantity': int
        }]]): quantity of each sku needing to be packed
        flat_rate_okay (boolean): whether or not usps flat and regional rate
            boxes can be used
        zone (int): usps regional shipping zone based on shotput Warehouse
        preferred_max_weight (int): max weight of a parcel if not 70lbs

    Example:
    >>> shotput_db_packing_algorithm(session, team1, {sku1: 1, sku2: 3}, True)
    {
        'package': (box=<best_standard_box object>,
                    skus_per_box= [[sku1, sku2], [sku2, sku2]],
                    last_parcel=<smaller_box object),
        'flat_rate': (box=<best_flat_rate object>,
                      skus_per_box=[[sku1], [sku2, sku2, sku2]],
                      last_parcel=None)
    }
    '''
    unordered_skus = []
    max_weight = preferred_max_weight or 31710
    min_box_dimensions = [None, None, None]
    weight = compute_package_weight(qty_per_sku)

    for sku_number, sku_data in qty_per_sku.iteritems():

        dimensions = sorted([sku_data['sku'].width_cm,
                             sku_data['sku'].height_cm,
                             sku_data['sku'].length_cm])
        min_box_dimensions = [max(a, b) for a, b in izip(dimensions,
                                                         min_box_dimensions)]
        unordered_skus += ([SkuTuple(sku_data['sku'], dimensions)] *
                           int(sku_data['quantity']))

    useable_boxes = select_useable_boxes(session, min_box_dimensions, team,
                                         flat_rate_okay)
     # if weight is greater than max, make sure we are separating it into
    # multiple boxes
    min_boxes_by_weight = math.ceil(weight / max_weight)

    if len(useable_boxes) == 0:
        raise BoxError(msg.boxes_too_small)

    return packing_algorithm(unordered_skus, useable_boxes, min_boxes_by_weight,
                             zone)

def compare_1000_times(trials=None):
    results = {
        'number_of_parcels': {
            'pyshipping': 0,
            'shotput': 0,
            'tie': 0,
            'errors': []
        },
        'when_tied': {
            'pyshipping': 0,
            'shotput': 0,
            'tie': 0,
            'all_in_one_bin': 0,
            'errors': []
        },
        'time_efficiency': {
            'pyshipping': 0,
            'shotput': 0,
            'tie': 0
        }
    }
    shotput_time = 0
    pyshipping_time = 0
    trials = int(trials or 1000)
    parcels_diff = []
    percent_saved = []
    for _ in xrange(trials):
        returned = compare_pyshipping_with_shotput()
        results['number_of_parcels'][returned['best_results']] +=1
        # interpret data when there is a tie
        if returned['best_results'] == 'tie':
            shotput_last_parcel = returned['shotput']['skus_per_parcel'][-1]
            py_last_parcel = returned['pyshipping']['skus_per_parcel'][-1]
            if shotput_last_parcel > py_last_parcel:
                winner = 'pyshipping'
                # results['when_tied']['errors'].append(returned)
            elif shotput_last_parcel < py_last_parcel:
                winner = 'shotput'
            else:
                winner = 'tie'
                if returned['shotput']['num_parcels'] == 1:
                    results['when_tied']['all_in_one_bin'] +=1
            results['when_tied'][winner] +=1
        # if returned['best_results'] == 'pyshipping':
        #     results['number_of_parcels']['errors'].append(returned)
        fastest = ('shotput', 'pyshipping', 'tie')[(
            returned['shotput']['time'] > returned['pyshipping']['time'])
            + (returned['shotput']['time'] <= returned['pyshipping']['time'])]
        results['time_efficiency'][fastest] += 1
        shotput_time += returned['shotput']['time']
        pyshipping_time += returned['pyshipping']['time']
        saved = (returned['pyshipping']['num_parcels']
                 - returned['shotput']['num_parcels'])
        if not (returned['best_results'] == 'tie'
                and returned['shotput']['num_parcels'] == 1):
            parcels_diff.append(saved)
            percent_saved.append(float(saved) /
                                 returned['pyshipping']['num_parcels'])

    # regression analysis
    parcels_diff = sorted(parcels_diff)
    percent_saved = sorted(percent_saved)
    parcels_diff_regression = {}
    percent_saved_regression = {}
    sample_size = trials - results['when_tied']['all_in_one_bin']
    parcels_diff_regression['mean'] = sum(parcels_diff) / float(sample_size)
    percent_saved_regression['mean'] = sum(percent_saved) / float(sample_size)
    parcels_diff_regression['median'] = parcels_diff[sample_size / 2 - 1]
    percent_saved_regression['median'] = percent_saved[sample_size / 2 - 1]
    parcels_diff_regression['standard_deviation'] = math.sqrt(sum(
        math.pow(x - parcels_diff_regression['mean'], 2) for x in parcels_diff)
        * (1.0 / float(sample_size)))
    percent_saved_regression['standard_deviation'] = math.sqrt(sum(
        math.pow(x - percent_saved_regression['mean'], 2) for x in percent_saved)
        * (1.0 / float(sample_size)))
    results['parcels_diff_regression'] = parcels_diff_regression
    results['percent_saved_regression'] = percent_saved_regression
    results['shotput_time_avg'] = shotput_time / float(trials)
    results['pyshipping_time_avg'] = pyshipping_time / float(trials)
    return results


def compare_pyshipping_with_shotput():
    from random import randint
    from pyshipping import binpack_simple as binpack
    from pyshipping.package import Package
    from time import time
    skus = []
    py_skus = []
    box_dims = sorted([randint(100, 200), randint(100, 200), randint(100, 200)])
    num_skus = 500
    for _ in xrange(num_skus):
        sku_dims = sorted([randint(20, 100), randint(20, 100), randint(20, 100)])
        skus.append(SkuTuple(str(volume(sku_dims)), sku_dims))
        py_skus.append(Package((sku_dims[0], sku_dims[1], sku_dims[2]), 0))
    start = time()
    skus_packed = pack_boxes(box_dims, skus)
    end = time()
    shotput = {
        'num_parcels': len(skus_packed),
        'skus_per_parcel': [len(parcel) for parcel in skus_packed],
        'time': end - start
    }
    py_box = Package((box_dims[0], box_dims[1], box_dims[2]), 0)
    start = time()
    py_skus_packed = binpack.packit(py_box, py_skus)
    end = time()
    pyshipping = {
        'num_parcels': len(py_skus_packed[0]),
        'skus_per_parcel': [len(parcel) for parcel in py_skus_packed[0]],
        'time': end - start
    }
    if len(skus_packed) > len(py_skus_packed[0]):
        best_results = 'pyshipping'
    elif len(skus_packed) < len(py_skus_packed[0]):
        best_results = 'shotput'
    else:
        best_results = 'tie'
    return {'shotput': shotput,
            'pyshipping': pyshipping,
            'best_results': best_results}
