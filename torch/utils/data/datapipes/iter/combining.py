from collections import defaultdict, deque
from torch.utils.data import IterDataPipe
from typing import Dict, Iterator, Optional, Sequence, Sized, Tuple, TypeVar

T_co = TypeVar('T_co', covariant=True)


class UnzipIterDataPipe(IterDataPipe):
    r""" :class:`UnzipIterDataPipe`.

    The inverse of :class:`ZipIterDataPipe`, this DataPipe disaggregates
    the elements of the zipped iterable DataPipe. The first element
    determines the number of output DataPipe. A :class:`ZipIterDataPipe`
    is often attached later to aggregate these output DataPipes back.
    Return a tuple of split DataPipes.

    Example:
        >>> print(list(dp))
        [(1, 2), (3, 4), (5, 6), ...]
        >>> dp1, dp2 = datapipes.iter.Unzip(dp)
        >>> print(list(dp1))
        [1, 3, 5, ...]
        >>> print(list(dp2))
        [2, 3, 4, ...]
        >>> def fn(data):
        ...     return torch.tensor(data)
        >>> map_dp = datapipes.iter.Map(dp1, fn=fn)
        >>> zipped_dp = datapipes.iter.Zip(map_dp, dp2)
        >>> print(list(zipped_dp))
        [(tensor(1), 2), (tensor(3), 4), (tensor(5), 6), ...]

    .. note:: This DataPipe may require significant storage, especially
              when one iterator uses most of the data before another
              iterator starts.

    args:
        datapipe: Iterable DataPipe being disaggregated
    """
    datapipe: IterDataPipe[Sequence]
    num_splits: int
    _split_stopped: Dict[int, bool]
    _all_stopped: bool
    _it: Iterator[Sequence]

    def __new__(cls, datapipe: IterDataPipe[Sequence]):
        source_dp = super().__new__(cls)
        # TODO(Erjia): Valid type inference mechanism without fetching data
        #              at construction time
        it = iter(datapipe)
        data = next(it)
        num_splits = len(data)
        source_dp.__init__(datapipe, num_splits, it, data)
        return tuple(_SplitIterDataPipe(source_dp, i) for i in range(num_splits))

    def __init__(self, datapipe, num_splits, first_it, first_data):
        self.datapipe = datapipe
        self.num_splits = num_splits
        # Status to check if the split has finished processing
        # End: True
        # In-process: False
        self._split_stopped = {sp: True for sp in range(self.num_splits)}
        # Flag to check if all splits have finished processing.
        # It prevents the other splits resetting the iterator after
        # one split has already called reset() and the iterator has
        # already been re-initialized.
        self._all_stopped = True
        self._buffer = defaultdict(deque)
        self._it = first_it
        self._first_data = first_data

    def get(self, split_id):
        if len(self._buffer[split_id]) > 0:
            return self._buffer[split_id].popleft()
        try:
            data = next(self._it)
        except StopIteration:
            self._split_stopped[split_id] = True
            # Set the flag whenever all splits have finished processing
            if all(self._split_stopped.values()):
                self._all_stopped = True
            raise StopIteration
        if not isinstance(data, Sequence):
            raise RuntimeError("Each element from `datapipe` is required being "
                               "a Sequence, but {} is found.".format(type(data)))
        if len(data) != self.num_splits:
            raise RuntimeError("Each element from `datapipe` is required having "
                               "equal length ({} vs {})."
                               .format(self.num_splits, len(data)))
        for i in range(self.num_splits):
            self._buffer[i].append(data[i])
        return self.get(split_id)

    def reset(self, split_id):
        if not self._split_stopped[split_id] or not self._all_stopped:
            raise RuntimeError("Can not reset `UnzipIterDataPipe` when it's "
                               "still in process.")
        else:
            # The first split being re-initialized will reset the
            # iterator over the source DataPipe
            if all(self._split_stopped.values()):
                if self._first_data is not None:
                    for i in range(self.num_splits):
                        self._buffer[i].append(self._first_data[i])
                    self._first_data = None
                else:
                    self._it = iter(self.datapipe)
            self._split_stopped[split_id] = False
            # The last split being re-initialized will change flag
            # showing all splits have re-started.
            if not any(self._split_stopped.values()):
                self._all_stopped = False

    def __len__(self) -> int:
        if isinstance(self.datapipe, Sized) and len(self.datapipe) >= 0:
            return len(self.datapipe)
        raise NotImplementedError


class _SplitIterDataPipe(IterDataPipe[T_co]):
    r""" :class:`_SplitIterDataPipe`.

    Iterable DataPipe yields data for the corresponding element
    form the source zipped DataPipe. Only when all splits are
    exhausted, could the :class:`UnzipIterDataPipe` be reset for
    a new iteration.
    args:
        datapipe: UnzipIterDataPipe as the source for all splits
        split_id: The i-th element from the zipped DataPipe
    """
    datapipe: UnzipIterDataPipe
    split_id: int

    def __init__(self, datapipe: UnzipIterDataPipe, split_id: int):
        super().__init__()
        self.datapipe = datapipe
        self.split_id = split_id

    def __iter__(self) -> Iterator[T_co]:
        # Reset UnzipIterDataPipe iterator
        self.datapipe.reset(self.split_id)
        while True:
            try:
                yield self.datapipe.get(self.split_id)
            except StopIteration:
                break

    def __len__(self) -> int:
        if isinstance(self.datapipe, Sized) and len(self.datapipe) >= 0:
            return len(self.datapipe)
        raise NotImplementedError


class ZipIterDataPipe(IterDataPipe[Tuple[T_co]]):
    r""" :class:`ZipIterDataPipe`.

    Iterable DataPipe aggregates elements into a tuple from each of
    the input DataPipe. The output DataPipe is stopped when the
    shortest input DataPipe is exhausted. This DataPipe is often
    attached after :class:`UnzipIterDataPipe` to aggregate elements
    from the source DataPipe.
    args:
        *datapipes: Iterable DataPipes being aggregated
    """
    datapipes: Tuple[IterDataPipe]
    length: Optional[int]

    def __init__(self, *datapipes: IterDataPipe):
        if not all(isinstance(dp, IterDataPipe) for dp in datapipes):
            raise TypeError("All inputs are required to be `IterDataPipe` "
                            "for `ZipIterDataPipe`.")
        super().__init__()
        self.datapipes = datapipes  # type: ignore
        self.length = None

    def __iter__(self) -> Iterator[Tuple[T_co]]:
        for data in zip(*self.datapipes):
            yield data

    def __len__(self) -> int:
        if self.length is not None:
            if self.length == -1:
                raise NotImplementedError
            return self.length
        if all(isinstance(dp, Sized) for dp in self.datapipes):
            self.length = min(len(dp) for dp in self.datapipes)  # type: ignore
        else:
            self.length = -1
        return len(self)
