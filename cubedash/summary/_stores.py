from __future__ import absolute_import

import functools
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Iterable, Dict
from typing import Optional

import dateutil.parser
import structlog
from dataclasses import dataclass
from geoalchemy2 import shape as geo_shape
from sqlalchemy import DDL, \
    and_
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql as postgres
from sqlalchemy.engine import Engine

from cubedash import _utils
from cubedash._utils import alchemy_engine
from cubedash.summary import _extents, TimePeriodOverview
from cubedash.summary import _schema
from cubedash.summary._schema import DATASET_SPATIAL, TIME_OVERVIEW, PRODUCT, PgGridCell
from cubedash.summary._summarise import Summariser
from datacube.index import Index
from datacube.model import DatasetType
from datacube.model import Range

_LOG = structlog.get_logger()


@dataclass
class ProductSummary:
    name: str
    dataset_count: int
    # Null when dataset_count == 0
    time_earliest: Optional[datetime]
    time_latest: Optional[datetime]

    # How long ago the spatial extents for this product were last refreshed.
    # (Field comes from DB on load)
    last_refresh_age: Optional[timedelta] = None

    id_: Optional[int] = None


class SummaryStore:
    def __init__(self, index: Index, summariser: Summariser, log=_LOG) -> None:
        self.index = index
        self.log = log
        self._update_listeners = []

        self._engine: Engine = alchemy_engine(index)
        self._summariser = summariser

    @classmethod
    def create(cls, index: Index, log=_LOG) -> 'SummaryStore':
        return cls(index, Summariser(alchemy_engine(index)), log=log)

    def init(self,
             init_products=True,
             refresh_older_than: timedelta = timedelta(days=1)):
        _schema.METADATA.create_all(self._engine, checkfirst=True)
        if init_products:
            for product in self.index.products.get_all():
                self.init_product(product, refresh_older_than=refresh_older_than)

    def init_product(self,
                     product: DatasetType,
                     refresh_older_than: timedelta = timedelta(days=1)):
        our_product = self._get_product(product.name)

        if (our_product is not None and
                our_product.last_refresh_age < refresh_older_than):
            _LOG.debug(
                'init.product.skip.too_recent',
                product_name=product.name,
                age=our_product.last_refresh_age
            )
            return None

        _LOG.debug('init.product', product_name=product.name)
        added_count = _extents.refresh_product(self.index, product)
        earliest, latest, total_count = self._engine.execute(
            select((
                func.min(DATASET_SPATIAL.c.center_time),
                func.max(DATASET_SPATIAL.c.center_time),
                func.count(),
            )).where(DATASET_SPATIAL.c.dataset_type_ref == product.id)
        ).fetchone()
        self._set_product_extent(
            ProductSummary(
                product.name,
                total_count,
                earliest,
                latest,
            )
        )
        return added_count

    def drop_all(self):
        """
        Drop all cubedash-specific tables/schema.
        """
        self._engine.execute(
            DDL(f'drop schema if exists {_schema.CUBEDASH_SCHEMA} cascade')
        )

    def get(self, product_name: Optional[str], year: Optional[int],
            month: Optional[int], day: Optional[int]) -> Optional[TimePeriodOverview]:

        start_day, period = self._start_day(year, month, day)

        product = self._get_product(product_name)
        if not product:
            return None

        res = self._engine.execute(
            select([TIME_OVERVIEW]).where(
                and_(
                    TIME_OVERVIEW.c.product_ref == product.id_,
                    TIME_OVERVIEW.c.start_day == start_day,
                    TIME_OVERVIEW.c.period_type == period,
                )
            )
        ).fetchone()

        if not res:
            return None

        return _summary_from_row(res)

    def _start_day(self, year, month, day):
        period = 'all'
        if year:
            period = 'year'
        if month:
            period = 'month'
        if day:
            period = 'day'

        return date(year or 1900, month or 1, day or 1), period

    @functools.lru_cache()
    def _get_product(self, name: str) -> Optional[ProductSummary]:
        row = self._engine.execute(
            select([
                PRODUCT.c.dataset_count,
                PRODUCT.c.time_earliest,
                PRODUCT.c.time_latest,
                (func.now() - PRODUCT.c.last_refresh).label("last_refresh_age"),
                PRODUCT.c.id.label("id_"),
            ]).where(PRODUCT.c.name == name)
        ).fetchone()
        if row:
            return ProductSummary(name=name, **row)
        else:
            return None

    def _set_product_extent(self, product: ProductSummary):

        fields = dict(
            dataset_count=product.dataset_count,
            time_earliest=product.time_earliest,
            time_latest=product.time_latest,
            # Deliberately do all age calculations with the DB clock rather than local.
            last_refresh=func.now(),
        )
        row = self._engine.execute(
            postgres.insert(
                PRODUCT
            ).on_conflict_do_update(
                index_elements=['name'],
                set_=fields
            ).values(
                name=product.name,
                **fields,
            )
        ).inserted_primary_key
        self._get_product.cache_clear()
        return row[0]

    def _put(self, product_name: Optional[str], year: Optional[int],
             month: Optional[int], day: Optional[int], summary: TimePeriodOverview):
        product = self._get_product(product_name)
        if not product:
            raise ValueError("Unknown product %r" % product_name)

        start_day, period = self._start_day(year, month, day)
        row = _summary_to_row(summary)
        self._engine.execute(
            postgres.insert(TIME_OVERVIEW).on_conflict_do_update(
                index_elements=[
                    'product_ref', 'start_day', 'period_type'
                ],
                set_=row,
                where=and_(
                    TIME_OVERVIEW.c.product_ref == product.id_,
                    TIME_OVERVIEW.c.start_day == start_day,
                    TIME_OVERVIEW.c.period_type == period,
                ),
            ).values(
                product_ref=product.id_,
                start_day=start_day,
                period_type=period,
                **row
            )
        )

    def has(self,
            product_name: Optional[str],
            year: Optional[int],
            month: Optional[int],
            day: Optional[int]) -> bool:
        return self.get(product_name, year, month, day) is not None

    def get_dataset_footprints(self,
                               product_name: Optional[str],
                               year: Optional[int],
                               month: Optional[int],
                               day: Optional[int]) -> Dict:
        """
        Return a GeoJSON FeatureCollection of each dataset footprint in the time range.

        Each Dataset is a separate GeoJSON Feature (with embedded properties for id and tile/grid).
        """
        return self._summariser.get_dataset_footprints(product_name, _utils.as_time_range(year, month, day))

    def get_or_update(self,
                      product_name: Optional[str],
                      year: Optional[int],
                      month: Optional[int],
                      day: Optional[int]):
        """
        Get a cached summary if exists, otherwise generate one

        Note that generating one can be *extremely* slow.
        """
        summary = self.get(product_name, year, month, day)
        if summary:
            return summary
        else:
            summary = self.update(product_name, year, month, day)
            return summary

    def update(self,
               product_name: Optional[str],
               year: Optional[int],
               month: Optional[int],
               day: Optional[int],
               generate_missing_children=True):
        """Update the given summary and return the new one"""
        product = self._get_product(product_name)
        if not product:
            raise ValueError("Unknown product (initialised?)")

        get_child = self.get_or_update if generate_missing_children else self.get

        if year and month and day:
            # Don't store days, they're quick.
            return self._summariser.calculate_summary(
                product_name,
                _utils.as_time_range(year, month, day)
            )
        elif year and month:
            summary = self._summariser.calculate_summary(
                product_name,
                _utils.as_time_range(year, month),
            )
        elif year:
            summary = TimePeriodOverview.add_periods(
                get_child(product_name, year, month_, None)
                for month_ in range(1, 13)
            )
        elif product_name:
            if product.dataset_count > 0:
                years = range(product.time_earliest.year, product.time_latest.year + 1)
            else:
                years = []
            summary = TimePeriodOverview.add_periods(
                get_child(product_name, year_, None, None)
                for year_ in years
            )
        else:
            summary = TimePeriodOverview.add_periods(
                get_child(product.name, None, None, None)
                for product in self.index.products.get_all()
            )

        self._do_put(product_name, year, month, day, summary)

        for listener in self._update_listeners:
            listener(product_name, year, month, day, summary)
        return summary

    def _do_put(self, product_name, year, month, day, summary):

        # Don't bother storing empty periods that are outside of the existing range.
        # This doesn't have to be exact (note that someone may update in parallel too).
        if summary.dataset_count == 0 and (year or month):
            product_extent = self.get(product_name, None, None, None)
            if (not product_extent) or (not product_extent.time_range):
                return

            start, end = product_extent.time_range
            if datetime(year, month or 1, day or 1) < start:
                return
            if datetime(year, month or 12, day or 28) > end:
                return

        self._put(product_name, year, month, day, summary)

    def list_complete_products(self) -> Iterable[str]:
        """
        List products with summaries available.
        """
        all_products = self.index.datasets.types.get_all()
        existing_products = sorted(
            (
                product.name for product in all_products
                if self.has(product.name, None, None, None)
            )
        )
        return existing_products

    def get_last_updated(self) -> Optional[datetime]:
        """Time of last update, if known"""
        return None


def _safe_read_date(d):
    if d:
        return _utils.default_utc(dateutil.parser.parse(d))

    return None


def _summary_from_row(res):
    timeline_dataset_counts = Counter(
        dict(
            zip(res['timeline_dataset_start_days'], res['timeline_dataset_counts']))
    ) if res['timeline_dataset_start_days'] else None
    grid_dataset_counts = Counter(
        dict(
            zip(res['grid_dataset_grids'], res['grid_dataset_counts']))
    ) if res['grid_dataset_grids'] else None

    return TimePeriodOverview(
        dataset_count=res['dataset_count'],
        # : Counter
        timeline_dataset_counts=timeline_dataset_counts,
        grid_dataset_counts=grid_dataset_counts,
        timeline_period=res['timeline_period'],
        # : Range
        time_range=Range(res['time_earliest'], res['time_latest'])
        if res['time_earliest'] else None,
        # shapely.geometry.base.BaseGeometry
        footprint_geometry=(
            None if res['footprint_geometry'] is None
            else geo_shape.to_shape(res['footprint_geometry'])
        ),
        size_bytes=res['size_bytes'],
        footprint_count=res['footprint_count'],
        # The most newly created dataset
        newest_dataset_creation_time=res['newest_dataset_creation_time'],
        # When this summary was last generated
        summary_gen_time=res['generation_time'],
        crses=set(res['crses']) if res['crses'] is not None else None,
    )


def _summary_to_row(summary: TimePeriodOverview) -> dict:
    counts = summary.timeline_dataset_counts
    day_counts = day_values = grid_counts = grid_values = None
    if counts:
        day_values, day_counts = zip(
            *sorted(summary.timeline_dataset_counts.items())
        )
        grid_values, grid_counts = zip(
            *sorted(summary.grid_dataset_counts.items())
        )

    begin, end = summary.time_range if summary.time_range else (None, None)
    return dict(
        dataset_count=summary.dataset_count,
        timeline_dataset_start_days=day_values,
        timeline_dataset_counts=day_counts,

        # TODO: SQLALchemy needs a bit of type help for some reason. Possible PgGridCell bug?
        grid_dataset_grids=func.cast(grid_values, type_=postgres.ARRAY(PgGridCell)),
        grid_dataset_counts=grid_counts,

        timeline_period=summary.timeline_period,

        time_earliest=begin,
        time_latest=end,

        size_bytes=summary.size_bytes,

        footprint_geometry=(
            None if summary.footprint_geometry is None
            else geo_shape.from_shape(summary.footprint_geometry)
        ),
        footprint_count=summary.footprint_count,

        newest_dataset_creation_time=summary.newest_dataset_creation_time,
        generation_time=summary.summary_gen_time,
        crses=summary.crses
    )
