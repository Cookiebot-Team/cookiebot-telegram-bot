"""Mojo port of the cb_core hot path (cooldowns, dedupe, textmatch).

Mirrors packages/cb-core/src/cb_core/{cooldowns,dedupe,textmatch}.py so the two
compiled builds can be benchmarked against the same workload. Core logic lives
in plain Mojo methods (`*_impl`); the `@staticmethod` wrappers exist only to
cross the CPython boundary, which lets the same structs be benchmarked with and
without FFI cost.
"""

from std.os import abort
from std.python import Python, PythonObject
from std.python.bindings import PythonModuleBuilder
from std.collections import Dict, Set


# ---------------------------------------------------------------- cooldowns


struct TokenBucket(Defaultable, Movable, Writable):
    var capacity: Float64
    var rate: Float64
    var tokens: Float64
    var last: Float64

    def write_to(self, mut writer: Some[Writer]):
        writer.write("TokenBucket(capacity=", self.capacity, ", rate=", self.rate, ")")

    def __init__(out self):
        self.capacity = 0.0
        self.rate = 0.0
        self.tokens = 0.0
        self.last = 0.0

    def __init__(out self, capacity: Float64, rate: Float64, now: Float64 = 0.0):
        self.capacity = capacity
        self.rate = rate
        self.tokens = capacity
        self.last = now

    @always_inline
    def allow_impl(mut self, now: Float64, cost: Float64 = 1.0) -> Bool:
        var elapsed = now - self.last
        if elapsed > 0.0:
            self.last = now
            var tokens = self.tokens + elapsed * self.rate
            self.tokens = tokens if tokens < self.capacity else self.capacity
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def retry_after_impl(self, cost: Float64 = 1.0) raises -> Float64:
        var deficit = cost - self.tokens
        if deficit <= 0.0:
            return 0.0
        if self.rate <= 0.0:
            return Float64("inf")
        return deficit / self.rate

    @staticmethod
    def py_init(out self: TokenBucket, args: PythonObject, kwargs: PythonObject) raises:
        var capacity: Float64 = 0.0
        var rate: Float64 = 0.0
        var now: Float64 = 0.0
        if len(args) >= 1:
            capacity = Float64(py=args[0])
        if len(args) >= 2:
            rate = Float64(py=args[1])
        if len(args) >= 3:
            now = Float64(py=args[2])
        if "capacity" in kwargs:
            capacity = Float64(py=kwargs["capacity"])
        if "rate" in kwargs:
            rate = Float64(py=kwargs["rate"])
        if "now" in kwargs:
            now = Float64(py=kwargs["now"])
        self = Self(capacity, rate, now)

    @staticmethod
    def allow(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], now: PythonObject
    ) raises -> PythonObject:
        return self_ptr[].allow_impl(Float64(py=now))

    @staticmethod
    def retry_after(self_ptr: UnsafePointer[Self, MutAnyOrigin]) raises -> PythonObject:
        return self_ptr[].retry_after_impl()

    @staticmethod
    def tokens_left(self_ptr: UnsafePointer[Self, MutAnyOrigin]) raises -> PythonObject:
        return self_ptr[].tokens

    @staticmethod
    def noop(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], value: PythonObject
    ) raises -> PythonObject:
        """Measures the Python -> Mojo call itself: one float in, one bool out."""
        return Float64(py=value) > 0.0


struct SlidingWindow(Defaultable, Movable, Writable):
    """Same semantics as the Python/Cython version: drop-from-front list."""

    var limit: Int
    var window: Float64
    var stamps: List[Float64]

    def write_to(self, mut writer: Some[Writer]):
        writer.write("SlidingWindow(limit=", self.limit, ", n=", len(self.stamps), ")")

    def __init__(out self):
        self.limit = 0
        self.window = 0.0
        self.stamps = []

    def __init__(out self, limit: Int, window: Float64):
        self.limit = limit
        self.window = window
        self.stamps = []

    @always_inline
    def hit_impl(mut self, now: Float64) -> Int:
        var cutoff = now - self.window
        var drop = 0
        var n = len(self.stamps)
        var cap = self.limit * 4
        while drop < n and self.stamps[drop] <= cutoff:
            drop += 1
        if drop > 0:
            self._drop_front(drop)
        self.stamps.append(now)
        n = len(self.stamps)
        if n > cap:
            self._drop_front(n - cap)
            n = cap
        return n

    def _drop_front(mut self, count: Int):
        var n = len(self.stamps)
        var i = 0
        while i + count < n:
            self.stamps[i] = self.stamps[i + count]
            i += 1
        while len(self.stamps) > n - count:
            _ = self.stamps.pop()

    @always_inline
    def exceeded_impl(mut self, now: Float64) -> Bool:
        return self.hit_impl(now) > self.limit

    @staticmethod
    def py_init(out self: SlidingWindow, args: PythonObject, kwargs: PythonObject) raises:
        var limit: Int = 0
        var window: Float64 = 0.0
        if len(args) >= 1:
            limit = Int(py=args[0])
        if len(args) >= 2:
            window = Float64(py=args[1])
        if "limit" in kwargs:
            limit = Int(py=kwargs["limit"])
        if "window" in kwargs:
            window = Float64(py=kwargs["window"])
        self = Self(limit, window)

    @staticmethod
    def hit(self_ptr: UnsafePointer[Self, MutAnyOrigin], now: PythonObject) raises -> PythonObject:
        return self_ptr[].hit_impl(Float64(py=now))

    @staticmethod
    def exceeded(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], now: PythonObject
    ) raises -> PythonObject:
        return self_ptr[].exceeded_impl(Float64(py=now))

    @staticmethod
    def count(self_ptr: UnsafePointer[Self, MutAnyOrigin]) raises -> PythonObject:
        return len(self_ptr[].stamps)


struct QuotaLedger(Defaultable, Movable, Writable):
    var limit: Int
    var used: Dict[Int64, Int]
    var day: Int

    def write_to(self, mut writer: Some[Writer]):
        writer.write("QuotaLedger(limit=", self.limit, ", keys=", len(self.used), ")")

    def __init__(out self):
        self.limit = 0
        self.used = {}
        self.day = -1

    def __init__(out self, limit: Int):
        self.limit = limit
        self.used = {}
        self.day = -1

    @always_inline
    def take_impl(mut self, key: Int64, day_ordinal: Int, cost: Int = 1) -> Bool:
        if day_ordinal != self.day:
            self.day = day_ordinal
            self.used.clear()
        var used = self.used.get(key, 0)
        if used + cost > self.limit:
            return False
        self.used[key] = used + cost
        return True

    def remaining_impl(self, key: Int64, day_ordinal: Int) -> Int:
        if day_ordinal != self.day:
            return self.limit
        return self.limit - self.used.get(key, 0)

    @staticmethod
    def py_init(out self: QuotaLedger, args: PythonObject, kwargs: PythonObject) raises:
        var limit: Int = 0
        if len(args) >= 1:
            limit = Int(py=args[0])
        if "limit" in kwargs:
            limit = Int(py=kwargs["limit"])
        self = Self(limit)

    @staticmethod
    def take(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], key: PythonObject, day_ordinal: PythonObject
    ) raises -> PythonObject:
        return self_ptr[].take_impl(Int64(Int(py=key)), Int(py=day_ordinal))

    @staticmethod
    def remaining(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], key: PythonObject, day_ordinal: PythonObject
    ) raises -> PythonObject:
        return self_ptr[].remaining_impl(Int64(Int(py=key)), Int(py=day_ordinal))


# ------------------------------------------------------------------- dedupe


struct RecentIds(Defaultable, Movable, Writable):
    """Fixed-capacity LRU of seen update ids — ring buffer + membership set."""

    var capacity: Int
    var ids: Set[Int64]
    var order: List[Int64]
    var head: Int

    def write_to(self, mut writer: Some[Writer]):
        writer.write("RecentIds(", len(self.ids), "/", self.capacity, ")")

    def __init__(out self):
        self.capacity = 1
        self.ids = Set[Int64]()
        self.order = [Int64(0)]
        self.head = 0

    def __init__(out self, capacity: Int) raises:
        if capacity < 1:
            raise Error("capacity must be >= 1")
        self.capacity = capacity
        self.ids = Set[Int64]()
        self.order = List[Int64](length=capacity, fill=0)
        self.head = 0

    @always_inline
    def seen_impl(mut self, update_id: Int64) raises -> Bool:
        if update_id in self.ids:
            return True
        if len(self.ids) >= self.capacity:
            var victim = self.order[self.head]
            if victim in self.ids:
                self.ids.remove(victim)
        self.order[self.head] = update_id
        self.ids.add(update_id)
        var head = self.head + 1
        self.head = 0 if head >= self.capacity else head
        return False

    @staticmethod
    def py_init(out self: RecentIds, args: PythonObject, kwargs: PythonObject) raises:
        var capacity: Int = 65536
        if len(args) >= 1:
            capacity = Int(py=args[0])
        if "capacity" in kwargs:
            capacity = Int(py=kwargs["capacity"])
        self = Self(capacity)

    @staticmethod
    def seen(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], update_id: PythonObject
    ) raises -> PythonObject:
        return self_ptr[].seen_impl(Int64(Int(py=update_id)))

    @staticmethod
    def size(self_ptr: UnsafePointer[Self, MutAnyOrigin]) raises -> PythonObject:
        return len(self_ptr[].ids)

    @staticmethod
    def seen_many_count(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], update_ids: PythonObject
    ) raises -> PythonObject:
        """Same batch, but the result is one Int — isolates output-building cost."""
        var dupes = 0
        for update_id in update_ids:
            if self_ptr[].seen_impl(Int64(Int(py=update_id))):
                dupes += 1
        return dupes

    @staticmethod
    def seen_range(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], start: PythonObject, count: PythonObject
    ) raises -> PythonObject:
        """No Python collection at all: isolates the per-item PythonObject cost."""
        var first = Int(py=start)
        var n = Int(py=count)
        var dupes = 0
        for i in range(n):
            if self_ptr[].seen_impl(Int64(first + i)):
                dupes += 1
        return dupes

    @staticmethod
    def seen_many(
        self_ptr: UnsafePointer[Self, MutAnyOrigin], update_ids: PythonObject
    ) raises -> PythonObject:
        """One crossing for a whole getUpdates batch instead of one per update."""
        var out = Python.list()
        for update_id in update_ids:
            out.append(self_ptr[].seen_impl(Int64(Int(py=update_id))))
        return out


# ---------------------------------------------------------------- textmatch


comptime SPACE: UInt8 = 32
comptime SLASH: UInt8 = 47
comptime AT: UInt8 = 64
comptime ZERO: UInt8 = 48
comptime NINE: UInt8 = 57
comptime LOWER_D: UInt8 = 100


comptime UPPER_A: UInt8 = 65
comptime UPPER_Z: UInt8 = 90
comptime NON_ASCII: UInt8 = 128


@always_inline
def _is_ascii_space(c: UInt8) -> Bool:
    return c == SPACE or (c >= 9 and c <= 13)


@always_inline
def _needs_unicode_lower(s: String) -> Bool:
    """True if `.lower()` would do more than flip ASCII A-Z.

    `String.lower()` is full Unicode case folding and costs ~450 ns/op — two
    orders of magnitude more than everything else in `parse_impl`. Commands are
    ASCII in practice, so take the slow path only when a non-ASCII byte is
    actually present.
    """
    var b = s.as_bytes()
    for i in range(len(b)):
        if b[i] >= NON_ASCII:
            return True
    return False


@always_inline
def _has_upper_ascii(s: String) -> Bool:
    var b = s.as_bytes()
    for i in range(len(b)):
        if b[i] >= UPPER_A and b[i] <= UPPER_Z:
            return True
    return False


@always_inline
def _lower(var s: String) -> String:
    """`str.lower()` semantics, skipping the Unicode path when nothing changes.

    Already-lowercase ASCII — every alias in the table, and the overwhelming
    majority of real traffic — returns without touching the string.
    """
    if not _has_upper_ascii(s) and not _needs_unicode_lower(s):
        return s^
    return s.lower()


@always_inline
def _equal_ignoring_case(a: String, b: String) -> Bool:
    """`a.lower() == b.lower()` for the @bot suffix, without allocating.

    Telegram usernames are ASCII-only, so the byte loop is the whole story; the
    Unicode path stays reachable for anything else.
    """
    var ab = a.as_bytes()
    var bb = b.as_bytes()
    if _needs_unicode_lower(a) or _needs_unicode_lower(b):
        return a.lower() == b.lower()
    if len(ab) != len(bb):
        return False
    for i in range(len(ab)):
        var x = ab[i]
        var y = bb[i]
        if x >= UPPER_A and x <= UPPER_Z:
            x += 32
        if y >= UPPER_A and y <= UPPER_Z:
            y += 32
        if x != y:
            return False
    return True


struct CommandTable(Defaultable, Movable, Writable):
    """`parse_command` plus its alias table.

    The alias map is per-instance rather than a module global; a group's gateway
    builds one at startup, exactly like the Cython module-level dict.
    """

    var aliases: Dict[String, String]

    def write_to(self, mut writer: Some[Writer]):
        writer.write("CommandTable(", len(self.aliases), " aliases)")

    def __init__(out self):
        self.aliases = Dict[String, String]()
        self._fill()

    def _fill(mut self):
        # core
        self.aliases["commands"] = "commands"
        self.aliases["comandos"] = "commands"
        self.aliases["privacy"] = "privacy"
        self.aliases["privacidade"] = "privacy"
        self.aliases["privacidad"] = "privacy"
        self.aliases["rules"] = "rules"
        self.aliases["regras"] = "rules"
        self.aliases["reglas"] = "rules"
        self.aliases["newrules"] = "newrules"
        self.aliases["novasregras"] = "newrules"
        self.aliases["nuevasreglas"] = "newrules"
        self.aliases["newwelcome"] = "newwelcome"
        self.aliases["novobemvindo"] = "newwelcome"
        self.aliases["nuevabienvenida"] = "newwelcome"
        self.aliases["isalive"] = "isalive"
        self.aliases["tavivo"] = "isalive"
        self.aliases["config"] = "config"
        self.aliases["configure"] = "config"
        self.aliases["configurar"] = "config"
        # fun
        self.aliases["dice"] = "dice"
        self.aliases["dado"] = "dice"
        self.aliases["roll"] = "dice"
        self.aliases["ship"] = "ship"
        self.aliases["shipp"] = "ship"
        self.aliases["shippar"] = "ship"
        self.aliases["death"] = "death"
        self.aliases["morte"] = "death"
        self.aliases["muerte"] = "death"
        self.aliases["meme"] = "meme"
        self.aliases["battle"] = "battle"
        self.aliases["batalha"] = "battle"
        self.aliases["batalla"] = "battle"
        self.aliases["random"] = "random"
        self.aliases["aleatorio"] = "random"
        self.aliases["aleatório"] = "random"
        self.aliases["firecracker"] = "firecracker"
        self.aliases["rojao"] = "firecracker"
        self.aliases["rojão"] = "firecracker"
        self.aliases["acende"] = "firecracker"
        self.aliases["fogos"] = "firecracker"
        self.aliases["complaint"] = "complaint"
        self.aliases["milton"] = "complaint"
        self.aliases["reclamacao"] = "complaint"
        self.aliases["reclamação"] = "complaint"
        self.aliases["queja"] = "complaint"
        # util
        self.aliases["birthday"] = "birthday"
        self.aliases["aniversario"] = "birthday"
        self.aliases["aniversário"] = "birthday"
        self.aliases["cumpleanos"] = "birthday"
        self.aliases["cumpleaños"] = "birthday"
        self.aliases["nextbirthday"] = "nextbirthday"
        self.aliases["nextbirthdays"] = "nextbirthday"
        self.aliases["proximosaniversarios"] = "nextbirthday"
        self.aliases["everyone"] = "everyone"
        self.aliases["adm"] = "calladms"
        self.aliases["admin"] = "calladms"
        self.aliases["report"] = "calladms"
        self.aliases["youtube"] = "youtube"
        self.aliases["deletereposts"] = "deletereposts"
        self.aliases["deleteposts"] = "deletereposts"
        self.aliases["apagarposts"] = "deletereposts"
        self.aliases["publish"] = "publish"
        self.aliases["divulgar"] = "publish"
        self.aliases["publicar"] = "publish"
        self.aliases["repost"] = "repost"
        self.aliases["repostar"] = "repost"
        self.aliases["reenviar"] = "repost"
        # partnered conventions
        self.aliases["bff"] = "con_bff"
        self.aliases["patas"] = "con_patas"
        self.aliases["fursmeet"] = "con_fursmeet"
        self.aliases["trex"] = "con_trex"
        self.aliases["furcamp"] = "con_furcamp"
        self.aliases["pawstral"] = "con_pawstral"

    def parse_impl(
        self, text: String, bot_username: String
    ) raises -> Optional[Tuple[String, String, String]]:
        """Returns (name, args, target) or None. Mirrors textmatch.parse_command."""
        var b = text.as_bytes()
        var end = len(b)
        if end == 0 or b[0] != SLASH:
            return None

        var i = 1
        while i < end and not _is_ascii_space(b[i]):
            i += 1
        if i == 1:
            return None

        var head = String(text[byte=1:i])
        var args = String(String(text[byte=i:end]).strip())

        var target = String("")
        var at = head.find("@")
        if at >= 0:
            target = String(head[byte = at + 1 : head.byte_length()])
            head = String(head[byte=0:at])

        if target and bot_username and not _equal_ignoring_case(target, bot_username):
            return None

        var key = _lower(head^)
        var canonical = self.aliases.get(key, String(""))
        if not canonical:
            # /d20, /d6 dice shorthand — same "no digit cap" rule as v1.
            var kb = key.as_bytes()
            if len(kb) < 2 or kb[0] != LOWER_D:
                return None
            var j = 1
            while j < len(kb):
                if kb[j] < ZERO or kb[j] > NINE:
                    return None
                j += 1
            var sides = String(key[byte = 1 : key.byte_length()])
            if args:
                return (String("dice"), sides + " " + args, target)
            return (String("dice"), sides, target)
        return (canonical, args, target)

    @staticmethod
    def py_init(out self: CommandTable, args: PythonObject, kwargs: PythonObject) raises:
        self = Self()

    @staticmethod
    def parse_command(
        self_ptr: UnsafePointer[Self, MutAnyOrigin],
        text: PythonObject,
        bot_username: PythonObject,
    ) raises -> PythonObject:
        var parsed = self_ptr[].parse_impl(String(py=text), String(py=bot_username))
        if not parsed:
            return Python.none()
        var value = parsed.value()
        return Python.tuple(value[0], value[1], value[2])

    @staticmethod
    def parse_many(
        self_ptr: UnsafePointer[Self, MutAnyOrigin],
        texts: PythonObject,
        bot_username: PythonObject,
    ) raises -> PythonObject:
        var bot = String(py=bot_username)
        var out = Python.list()
        for text in texts:
            var parsed = self_ptr[].parse_impl(String(py=text), bot)
            if not parsed:
                out.append(Python.none())
            else:
                var value = parsed.value()
                out.append(Python.tuple(value[0], value[1], value[2]))
        return out


# ------------------------------------------------------------------ bindings


@export
def PyInit_cb_hot() -> PythonObject:
    try:
        var m = PythonModuleBuilder("cb_hot")
        _ = (
            m.add_type[TokenBucket]("TokenBucket")
            .def_py_init[TokenBucket.py_init]()
            .def_method[TokenBucket.allow]("allow")
            .def_method[TokenBucket.retry_after]("retry_after")
            .def_method[TokenBucket.tokens_left]("tokens_left")
            .def_method[TokenBucket.noop]("noop")
        )
        _ = (
            m.add_type[SlidingWindow]("SlidingWindow")
            .def_py_init[SlidingWindow.py_init]()
            .def_method[SlidingWindow.hit]("hit")
            .def_method[SlidingWindow.exceeded]("exceeded")
            .def_method[SlidingWindow.count]("count")
        )
        _ = (
            m.add_type[QuotaLedger]("QuotaLedger")
            .def_py_init[QuotaLedger.py_init]()
            .def_method[QuotaLedger.take]("take")
            .def_method[QuotaLedger.remaining]("remaining")
        )
        _ = (
            m.add_type[RecentIds]("RecentIds")
            .def_py_init[RecentIds.py_init]()
            .def_method[RecentIds.seen]("seen")
            .def_method[RecentIds.seen_many]("seen_many")
            .def_method[RecentIds.seen_many_count]("seen_many_count")
            .def_method[RecentIds.seen_range]("seen_range")
            .def_method[RecentIds.size]("size")
        )
        _ = (
            m.add_type[CommandTable]("CommandTable")
            .def_py_init[CommandTable.py_init]()
            .def_method[CommandTable.parse_command]("parse_command")
            .def_method[CommandTable.parse_many]("parse_many")
        )
        return m.finalize()
    except e:
        abort(String("failed to create module: ", e))
