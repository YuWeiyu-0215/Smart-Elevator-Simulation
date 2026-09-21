# -*- coding: utf-8 -*-
"""
红豆斋宿舍楼电梯群控调度仿真系统

用法：
    python elevator_simulation.py            # 跑 1 个随机种子
    python elevator_simulation.py 20         # 跑 20 个随机种子，报均值±波动
    python elevator_simulation.py 20 1.0     # 第 3 个参数 = 每人上下车耗时(秒)
"""

import csv
import math
import random
import sys
from dataclasses import dataclass, field


# ============================================================
# 一、参数（想改就改这里）
# ============================================================

FLOORS = 15
FLOOR_HEIGHT = 3.5

ACCELERATION = 0.6       # m/s²
MAX_SPEED = 1.75         # m/s

CAPACITY = 11

DOOR_OPEN = 2.6          # 秒
DOOR_CLOSE = 2.6         # 秒

TOTAL_TIME = 4 * 3600    # 4 小时
PEAK_START = 3 * 3600    # 第 3 小时结束后进入高峰
DRAIN_TIME = 20 * 60     # 4 小时结束后再跑 20 分钟，把没送完的人送完

JUNIOR_DELAY = 5         # 改造前：学弟刷闸机后平均 5 秒才按下按钮

SENIORS_PER_FLOOR = 20
JUNIORS_PER_FLOOR = 80

DT = 0.2
SEED = 42

T_PAX = 0.0              # 每人上下车耗时（秒）。0 = 不计入；可设 1.0 做敏感性分析

# 每隔多少秒重新评估一次尚未上车的呼梯，必要时改派更合适的电梯。
# 设 0 = 派梯后不再重算。
RE_EVAL_INTERVAL = 30

# 改造方案的 4 项核心措施（可逐项启用，用于分步对比实验）。
FEATURES_ALL = ("dispatch", "sensor", "predispatch", "face")

# 命令行参数：第 2 个 = 重复次数，第 3 个 = T_PAX；出现 ablate 就跑分步拆解
REPS = 1
ABLATE = "ablate" in sys.argv

_args = [a for a in sys.argv[1:] if a != "ablate"]
if len(_args) > 0:
    REPS = int(_args[0])
if len(_args) > 1:
    T_PAX = float(_args[1])


# ============================================================
# 二、电梯运行时间（梯形速度曲线）
# ============================================================

def travel_time(a, b):
    """电梯从 a 楼跑到 b 楼要多少秒。"""

    distance = abs(a - b) * FLOOR_HEIGHT

    if distance == 0:
        return 0

    # 加速到最高速再减速，一共需要的距离
    accel_decel_distance = MAX_SPEED ** 2 / ACCELERATION

    # 距离太短，根本加速不到最高速（三角形速度曲线）
    if distance <= accel_decel_distance:
        return 2 * math.sqrt(distance / ACCELERATION)

    # 距离够长：加速 + 匀速 + 减速（梯形速度曲线）
    t_acc = MAX_SPEED / ACCELERATION
    t_cruise = (distance - accel_decel_distance) / MAX_SPEED

    return 2 * t_acc + t_cruise


def group_direction(g):
    """这个人是想往上还是往下。1 = 上行，-1 = 下行。"""
    return 1 if g.destination > g.origin else -1


# ============================================================
# 三、学生和电梯
# ============================================================

@dataclass
class Group:
    """一个学生，或者一群结伴的学生。学长 size=1，学弟 size=2~5。"""

    id: int
    kind: str           # senior / junior
    size: int
    origin: int
    destination: int

    gate_time: float    # 刷闸机的时刻
    call_time: float    # 真正发出呼梯的时刻（改造前要晚 JUNIOR_DELAY 秒）

    board_time: float = None
    arrive_time: float = None
    elevator_id: int = None


@dataclass
class Elevator:
    id: int

    floor: int = 1
    direction: int = 0       # 1 上行，-1 下行，0 空闲
    target: int = None

    state: str = "idle"      # idle / moving / open / close
    timer: float = 0

    onboard: list = field(default_factory=list)   # 车上的人
    requests: set = field(default_factory=set)    # 派给这台梯的呼梯（小组 id）

    open_count: int = 0
    close_count: int = 0
    invalid_stops: int = 0
    moving_time: float = 0

    departure_loads: list = field(default_factory=list)  # 每次离开楼层时车上有几人
    silent_move: bool = False                            # 空车回 1 楼，不开门
    skip: set = field(default_factory=set)               # 短期内确认无人的楼层，本趟不再停靠


# ============================================================
# 四、仿真
# ============================================================

class Simulation:

    def __init__(self, improved=False, seed=SEED, features=None):
        self.improved = improved

        # features = 这一轮开启了哪几项改造措施
        if features is None:
            self.features = set(FEATURES_ALL) if improved else set()
        else:
            self.features = set(features)

        self.random = random.Random(seed)

        self.time = 0
        self.groups = {}
        self.order = []
        self.pending = set()      # 还没上车的组 id
        self.skip_count = 0       # 传感器跳过开门的次数

        self.elevators = [Elevator(1), Elevator(2), Elevator(3)]

        self.generate_people()

    def has(self, name):
        """这项改造措施开没开？"""
        return name in self.features

    # --------------------------------------------------------
    # 电梯服务范围（红豆斋现状：1号单、2号双、3号全层）
    # --------------------------------------------------------

    def can_serve(self, elevator, group):

        if elevator.id == 1:
            floors = {1, 3, 5, 7, 9, 11, 13, 15}
        elif elevator.id == 2:
            floors = {1, 2, 4, 6, 8, 10, 12, 14}
        else:
            floors = set(range(1, FLOORS + 1))

        return group.origin in floors and group.destination in floors

    # --------------------------------------------------------
    # 生成学生
    # --------------------------------------------------------

    def generate_people(self):

        gid = 1

        # 学长：2~8 层，每层 20 人，4 小时内均匀出现，只下楼
        for floor in range(2, 9):
            for _ in range(SENIORS_PER_FLOOR):
                t = self.random.uniform(0, TOTAL_TIME)
                self.groups[gid] = Group(
                    id=gid, kind="senior", size=1,
                    origin=floor, destination=1,
                    gate_time=t, call_time=t
                )
                gid += 1

        # 学弟：9~15 层，每层 80 人，2~5 人结伴，只在高峰出现，只上楼
        for floor in range(9, 16):
            remaining = JUNIORS_PER_FLOOR
            while remaining > 0:
                size = min(self.random.randint(2, 5), remaining)
                t = self.random.uniform(PEAK_START, TOTAL_TIME)

                # face = 刷脸自动呼梯，不用自己走过去按按钮
                call = t if self.has("face") else t + JUNIOR_DELAY

                self.groups[gid] = Group(
                    id=gid, kind="junior", size=size,
                    origin=1, destination=floor,
                    gate_time=t, call_time=call
                )
                gid += 1
                remaining -= size

        self.order = sorted(self.groups, key=lambda x: self.groups[x].call_time)
        self.pending = set(self.groups)
        self.next_eval = RE_EVAL_INTERVAL

    # --------------------------------------------------------
    # 改造后：给一次呼梯挑一台最合适的电梯
    # --------------------------------------------------------

    def score(self, elevator, group):
        """很好解释的评分：越近、活越少、人越少、方向越顺，分数越低。"""

        score = travel_time(elevator.floor, group.origin)
        score += len(self.get_stops(elevator)) * 4          # 中间还要停几次
        score += sum(g.size for g in elevator.onboard) * 0.8  # 车上已经多少人

        if elevator.direction != 0:
            desired = 1 if group.origin > elevator.floor else -1
            if desired != elevator.direction:
                score += 8

        return score

    def add_request(self, group):

        candidates = [e for e in self.elevators if self.can_serve(e, group)]

        if not candidates:
            return

        if self.has("dispatch"):
            # 群控派梯：每次只派一台梯。
            # 派梯目标是"前往该楼层接人"，而不是"接下这一个人"。
            best = min(candidates, key=lambda e: self.score(e, group))
            best.requests.add(group.id)
        else:
            # 改造前：相当于把能服务的按钮全按了一遍
            for e in candidates:
                e.requests.add(group.id)

        # 有新需求了，之前"这一层没人"的判断作废
        for e in self.elevators:
            e.skip.clear()

    # --------------------------------------------------------
    # 改造后：定期重新审视还没上车的呼梯
    # --------------------------------------------------------

    def re_evaluate(self):
        """
        呼梯请求的初始派梯并非一成不变：若原派电梯被其他任务占用，
        系统会将请求改派给当前更合适的电梯，避免机械执行初始指令。
        """

        changed = False

        for gid in list(self.pending):

            g = self.groups[gid]

            if g.call_time > self.time or g.elevator_id is not None:
                continue

            # 已经快到了就别乱动，免得两台梯一起跑过来
            current = [e for e in self.elevators if gid in e.requests]
            if any(e.floor == g.origin and e.state in ("open", "close")
                   for e in current):
                continue

            best = min(
                (e for e in self.elevators if self.can_serve(e, g)),
                key=lambda e: self.score(e, g),
                default=None
            )

            if best is None:
                continue

            # 当前派梯结果已非最优 → 改派
            if len(current) != 1 or current[0].id != best.id:
                for e in self.elevators:
                    e.requests.discard(gid)
                best.requests.add(gid)
                changed = True

        if changed:
            for e in self.elevators:
                e.skip.clear()

    # --------------------------------------------------------
    # 这台电梯现在应该停哪些楼层
    # --------------------------------------------------------

    def get_stops(self, elevator):
        """返回"在当前行进方向上"该停的楼层。这就是 LOOK 算法的核心。"""

        stops = set()

        # 车上的人的目的地 —— 永远要停
        for g in elevator.onboard:
            stops.add(g.destination)

        # 还没上车的人 —— 只停"顺路同方向"的
        for gid in elevator.requests:
            g = self.groups[gid]

            # 已送达或已被其他电梯接走的请求，直接跳过
            if g.arrive_time is not None:
                continue
            if g.elevator_id is not None:
                continue

            # 与当前运行方向不一致的请求，本趟不停靠，待返程时再接
            if elevator.direction != 0 and elevator.direction != group_direction(g):
                continue

            stops.add(g.origin)

        return stops

    # --------------------------------------------------------
    # 选下一个目标楼层
    # --------------------------------------------------------

    def choose_next(self, elevator):

        stops = self.get_stops(elevator) - elevator.skip

        # 全被跳过了：把跳过记录清掉，重新看一遍
        if not stops and elevator.skip:
            elevator.skip.clear()
            stops = self.get_stops(elevator)

        if not stops:
            elevator.target = None
            elevator.direction = 0
            elevator.state = "idle"
            return

        current = elevator.floor

        above = sorted(x for x in stops if x > current)
        below = sorted((x for x in stops if x < current), reverse=True)

        if elevator.direction == 1 and above:
            target = above[0]
        elif elevator.direction == -1 and below:
            target = below[0]
        elif above:
            target = above[0]
            elevator.direction = 1
        elif below:
            target = below[0]
            elevator.direction = -1
        else:
            target = current

        elevator.target = target

        if target == current:
            self.stop(elevator)
            return

        # 换方向了，之前的跳过记录作废
        elevator.skip.clear()

        elevator.state = "moving"
        elevator.timer = travel_time(current, target)

        elevator.departure_loads.append(
            sum(g.size for g in elevator.onboard)
        )

    # --------------------------------------------------------
    # 到达某一层
    # --------------------------------------------------------

    def stop(self, elevator):

        floor = elevator.floor

        # ---- 0. 清理已失效的呼梯请求 ----
        stale = set()
        for gid in elevator.requests:
            g = self.groups[gid]
            if g.arrive_time is not None:
                stale.add(gid)                       # 人早就到了
            elif g.elevator_id is not None and g.elevator_id != elevator.id:
                stale.add(gid)                       # 被别的梯接走了
        elevator.requests -= stale

        # ---- 1. 下客 ----
        dropped = []
        for g in elevator.onboard:
            if g.destination == floor:
                g.arrive_time = self.time
                dropped.append(g)
                # 人都到了，别的梯不用再惦记他
                for e in self.elevators:
                    e.requests.discard(g.id)

        elevator.onboard = [g for g in elevator.onboard if g not in dropped]

        # ---- 2. 上客（返回上车人数）----
        boarded = self.board(elevator)

        # ---- 3. 这一趟白停了吗 ----
        if not dropped and boarded == 0:
            elevator.invalid_stops += 1

            # sensor = 毫米波传感器：没人上下就别开门，直接走
            if self.has("sensor"):
                self.skip_count += 1
                elevator.skip.add(floor)
                elevator.target = None
                elevator.state = "idle"
                return

        # ---- 4. 开门 ----
        # 停站时间 = 开关门时间 + 乘客上下车时间
        people_moving = sum(g.size for g in dropped) + boarded

        elevator.open_count += 1
        elevator.state = "open"
        elevator.timer = DOOR_OPEN + T_PAX * people_moving

        elevator.target = None
        elevator.skip.clear()

    # --------------------------------------------------------
    # 上客
    # --------------------------------------------------------

    def board(self, elevator):
        """把这一层能带上的人带上，返回上车人数。"""

        floor = elevator.floor
        left = CAPACITY - sum(g.size for g in elevator.onboard)

        # 改造后：电梯到站即接走该层同方向的所有乘客，
        #         而不是只接当初派梯针对的那一组人。
        if self.has("dispatch"):
            pool = [self.groups[gid] for gid in self.pending]
        else:
            pool = [self.groups[gid] for gid in elevator.requests
                    if gid in self.pending]

        candidates = []
        for g in pool:

            if g.origin != floor:
                continue
            if g.call_time > self.time:      # 还没刷闸机呢
                continue
            if g.elevator_id is not None:    # 已经上车了
                continue
            if not self.can_serve(elevator, g):
                continue

            # 车上有人的时候，只接同方向的人（不然车里的人会被带着乱跑）
            if elevator.onboard and elevator.direction != 0:
                if elevator.direction != group_direction(g):
                    continue

            candidates.append(g)

        candidates.sort(key=lambda g: g.call_time)   # 先来先上

        boarded = 0
        for g in candidates:

            if g.size <= left:
                g.board_time = self.time
                g.elevator_id = elevator.id
                elevator.onboard.append(g)
                elevator.requests.add(g.id)
                self.pending.discard(g.id)
                boarded += g.size
                left -= g.size

                # 别的梯不用再来了
                for e in self.elevators:
                    if e.id != elevator.id:
                        e.requests.discard(g.id)

            if left <= 0:
                break

        return boarded

    # --------------------------------------------------------
    # 预调度：高峰期前把闲着的梯放回 1 楼
    # --------------------------------------------------------

    def pre_dispatch(self):

        if not self.has("predispatch"):
            return

        if self.time < PEAK_START - 60:
            return

        for e in self.elevators:
            if e.state == "idle" and not e.requests and e.floor != 1:

                e.target = 1
                e.direction = -1
                e.state = "moving"

                # 空车调度归位属于移动，不是载客停靠，因此不开门
                e.silent_move = True

                e.timer = travel_time(e.floor, 1)
                e.departure_loads.append(0)

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------

    def run(self):

        index = 0
        ordered = [self.groups[gid] for gid in self.order]

        while self.time <= TOTAL_TIME + DRAIN_TIME:

            # 新的呼梯
            while index < len(ordered) and ordered[index].call_time <= self.time:
                self.add_request(ordered[index])
                index += 1

            self.pre_dispatch()

            if self.has("dispatch") and RE_EVAL_INTERVAL and self.time >= self.next_eval:
                self.re_evaluate()
                self.next_eval += RE_EVAL_INTERVAL

            for e in self.elevators:

                if e.state == "moving":

                    e.timer -= DT
                    e.moving_time += DT

                    if e.timer <= 0:
                        e.floor = e.target

                        if e.silent_move:
                            e.target = None
                            e.direction = 0
                            e.state = "idle"
                            e.silent_move = False
                        else:
                            self.stop(e)

                elif e.state == "open":

                    e.timer -= DT

                    if e.timer <= 0:
                        e.state = "close"
                        e.timer = DOOR_CLOSE
                        e.close_count += 1

                elif e.state == "close":

                    e.timer -= DT

                    if e.timer <= 0:
                        e.state = "idle"
                        self.choose_next(e)

                elif e.state == "idle":

                    self.choose_next(e)

            self.time += DT

        return self.metrics()

    # --------------------------------------------------------
    # 百分位数
    # --------------------------------------------------------

    @staticmethod
    def percentile(values, p):

        if not values:
            return 0

        values = sorted(values)
        index = (len(values) - 1) * p

        low = int(index)
        high = math.ceil(index)

        if low == high:
            return values[low]

        return values[low] + (values[high] - values[low]) * (index - low)

    # --------------------------------------------------------
    # 统计
    # --------------------------------------------------------

    def metrics(self):

        waits = []        # 从"按下按钮"到"上车"
        gate_waits = []   # 从"刷闸机"到"上车"（更接近真实感受）
        rides = []

        waits_normal, waits_peak = [], []

        total_people = 0
        completed_people = 0

        for g in self.groups.values():

            total_people += g.size

            if g.board_time is None:
                continue

            wait = g.board_time - g.call_time
            gate_wait = g.board_time - g.gate_time

            for _ in range(g.size):
                waits.append(wait)
                gate_waits.append(gate_wait)

            # 平峰 / 高峰分开
            if g.call_time >= PEAK_START:
                for _ in range(g.size):
                    waits_peak.append(wait)
            else:
                for _ in range(g.size):
                    waits_normal.append(wait)

            if g.arrive_time is not None:
                completed_people += g.size
                ride = g.arrive_time - g.board_time
                for _ in range(g.size):
                    rides.append(ride)

        opens = sum(e.open_count for e in self.elevators)
        closes = sum(e.close_count for e in self.elevators)
        invalid = sum(e.invalid_stops for e in self.elevators)
        moving = sum(e.moving_time for e in self.elevators)

        loads = []
        for e in self.elevators:
            loads.extend(x for x in e.departure_loads if x > 0)

        return {
            "scenario": "改造后" if self.improved else "改造前",
            "total_people": total_people,
            "completed_people": completed_people,
            "completion_rate": completed_people / total_people * 100 if total_people else 0,

            "average_wait": sum(waits) / len(waits) if waits else 0,
            "p95_wait": self.percentile(waits, 0.95),
            "max_wait": max(waits) if waits else 0,

            "average_gate_wait": sum(gate_waits) / len(gate_waits) if gate_waits else 0,
            "p95_gate_wait": self.percentile(gate_waits, 0.95),

            "wait_normal": sum(waits_normal) / len(waits_normal) if waits_normal else 0,
            "wait_peak": sum(waits_peak) / len(waits_peak) if waits_peak else 0,
            "max_wait_peak": max(waits_peak) if waits_peak else 0,

            "average_ride": sum(rides) / len(rides) if rides else 0,

            "open_count": opens,
            "close_count": closes,
            "door_actions": opens + closes,
            "invalid_stops": invalid,
            "sensor_skips": self.skip_count,

            "average_load": sum(loads) / len(loads) if loads else 0,
            "utilization": moving / (TOTAL_TIME * 3) * 100,
        }


# ============================================================
# 五、多个随机种子取平均
# ============================================================

def average_results(results):

    keys = results[0].keys()
    out = {}

    for k in keys:
        if k == "scenario":
            out[k] = results[0][k]
            continue

        values = [r[k] for r in results]
        mean = sum(values) / len(values)

        if len(values) > 1:
            lo = min(values)
            hi = max(values)
            out[k] = mean
            out[k + "_lo"] = lo
            out[k + "_hi"] = hi
        else:
            out[k] = mean

    return out


def fmt(value, r, lo=None, hi=None):
    """把 "均值 (最低~最高)" 拼成字符串。"""
    if lo is None:
        return f"{value:.{r}f}"
    return f"{value:.{r}f}  ({lo:.{r}f} ~ {hi:.{r}f})"


def print_result(result, ranged):

    def g(k):
        return result.get(k + "_lo") if ranged else None

    def h(k):
        return result.get(k + "_hi") if ranged else None

    print()
    print("=" * 56)
    print(result["scenario"])
    print("=" * 56)

    print(f"总人数 / 送完：{result['total_people']:.0f} / "
          f"{result['completed_people']:.0f}  "
          f"(完成率 {fmt(result['completion_rate'], 2, g('completion_rate'), h('completion_rate'))}%)")

    print()
    print("【候梯时间：从按下按钮算起】")
    print(f"  平均：{fmt(result['average_wait'], 2, g('average_wait'), h('average_wait'))} s")
    print(f"  P95 ：{fmt(result['p95_wait'], 2, g('p95_wait'), h('p95_wait'))} s")
    print(f"  最长：{fmt(result['max_wait'], 2, g('max_wait'), h('max_wait'))} s")

    print()
    print("【候梯时间：从刷闸机算起（含找按钮/按按钮）】")
    print(f"  平均：{fmt(result['average_gate_wait'], 2, g('average_gate_wait'), h('average_gate_wait'))} s")
    print(f"  P95 ：{fmt(result['p95_gate_wait'], 2, g('p95_gate_wait'), h('p95_gate_wait'))} s")

    print()
    print("【分时段】")
    print(f"  平峰平均：{fmt(result['wait_normal'], 2, g('wait_normal'), h('wait_normal'))} s")
    print(f"  高峰平均：{fmt(result['wait_peak'], 2, g('wait_peak'), h('wait_peak'))} s")
    print(f"  高峰最长：{fmt(result['max_wait_peak'], 2, g('max_wait_peak'), h('max_wait_peak'))} s")

    print()
    print(f"平均乘梯时间：{fmt(result['average_ride'], 2, g('average_ride'), h('average_ride'))} s")
    print(f"开门次数    ：{fmt(result['open_count'], 0, g('open_count'), h('open_count'))}")
    print(f"开关门动作  ：{fmt(result['door_actions'], 0, g('door_actions'), h('door_actions'))}")
    print(f"无效停靠    ：{fmt(result['invalid_stops'], 0, g('invalid_stops'), h('invalid_stops'))}")
    print(f"传感器跳过  ：{fmt(result['sensor_skips'], 0, g('sensor_skips'), h('sensor_skips'))}")
    print(f"平均载客    ：{fmt(result['average_load'], 2, g('average_load'), h('average_load'))} 人")
    print(f"运行利用率  ：{fmt(result['utilization'], 2, g('utilization'), h('utilization'))} %")


# ============================================================
# 六、保存 CSV
# ============================================================

def save_csv(before, after):

    filename = "simulation_results.csv"

    fields = [k for k in before.keys() if not k.endswith(("_lo", "_hi"))]

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerow(before)
        writer.writerow(after)

    print()
    print(f"结果已保存到：{filename}")


# ============================================================
# 七、画图
# ============================================================

def draw(before, after):

    try:
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Arial"]
        plt.rcParams["axes.unicode_minus"] = False
    except ImportError:
        print()
        print("没装 matplotlib，跳过画图。需要的话：pip install matplotlib")
        return

    width = 0.35
    x = range(3)

    # 图 1：时间类指标
    names = ["平均候梯(s)", "P95候梯(s)", "最长候梯(s)"]
    old = [before["average_wait"], before["p95_wait"], before["max_wait"]]
    new = [after["average_wait"], after["p95_wait"], after["max_wait"]]

    plt.figure(figsize=(9, 5))
    plt.bar([i - width / 2 for i in x], old, width, label="改造前", color="#d9534f")
    plt.bar([i + width / 2 for i in x], new, width, label="改造后", color="#5cb85c")
    plt.xticks(list(x), names)
    plt.ylabel("秒")
    plt.title("候梯时间对比（按人数加权）")
    plt.legend()
    plt.tight_layout()
    plt.savefig("waiting_time_comparison.png", dpi=160)

    # 图 2：次数类指标（计数，别和人数画在一张图上）
    names = ["开门次数", "无效停靠", "开关门动作"]
    old = [before["open_count"], before["invalid_stops"], before["door_actions"]]
    new = [after["open_count"], after["invalid_stops"], after["door_actions"]]

    plt.figure(figsize=(9, 5))
    plt.bar([i - width / 2 for i in x], old, width, label="改造前", color="#d9534f")
    plt.bar([i + width / 2 for i in x], new, width, label="改造后", color="#5cb85c")
    plt.xticks(list(x), names)
    plt.ylabel("次")
    plt.title("运行效率对比")
    plt.legend()
    plt.tight_layout()
    plt.savefig("operation_comparison.png", dpi=160)

    # 图 3：平均载客单独成图，避免与开门次数共用坐标轴导致刻度失真
    plt.figure(figsize=(6, 5))
    plt.bar(["改造前", "改造后"],
            [before["average_load"], after["average_load"]],
            width=0.5, color=["#d9534f", "#5cb85c"])
    plt.ylabel("人")
    plt.title("平均每次出发载客人数")
    plt.tight_layout()
    plt.savefig("load_comparison.png", dpi=160)

    print("图表已保存：waiting_time_comparison.png / operation_comparison.png / load_comparison.png")


# ============================================================
# 八、分步拆解：到底是哪一项改造在起作用
# ============================================================

def ablation(seeds):

    ladder = [
        ("改造前（三项都按）", set()),
        ("① 三键合一统一派梯", {"dispatch"}),
        ("② ＋毫米波传感器", {"dispatch", "sensor"}),
        ("③ ＋空闲梯预调度", {"dispatch", "sensor", "predispatch"}),
        ("④ ＋刷脸免按键", set(FEATURES_ALL)),
    ]

    results = []

    for label, features in ladder:
        runs = [Simulation(seed=s, features=features).run() for s in seeds]

        def mean(key):
            return sum(r[key] for r in runs) / len(runs)

        results.append({
            "label": label,
            "average_wait": mean("average_wait"),
            "gate_wait": mean("average_gate_wait"),
            "p95_wait": mean("p95_wait"),
            "max_wait": mean("max_wait"),
            "wait_peak": mean("wait_peak"),
            "open_count": mean("open_count"),
            "invalid_stops": mean("invalid_stops"),
        })

    print()
    print("=" * 104)
    print(f"分步拆解：一项一项加改造措施（{len(seeds)} 个种子的平均）")
    print("=" * 104)

    print(f"{'阶段':<22}{'候梯(按键)':>11}{'候梯(闸机)':>12}"
          f"{'P95':>9}{'最长':>9}{'高峰平均':>10}{'开门次数':>10}{'无效停靠':>10}")
    print("-" * 104)

    for r in results:
        print(f"{r['label']:<22}"
              f"{r['average_wait']:>10.1f}s"
              f"{r['gate_wait']:>11.1f}s"
              f"{r['p95_wait']:>8.1f}s"
              f"{r['max_wait']:>8.1f}s"
              f"{r['wait_peak']:>9.1f}s"
              f"{r['open_count']:>10.0f}"
              f"{r['invalid_stops']:>10.0f}")

    print()
    print("怎么读这张表：")
    print("  · '候梯(按键)'从按下按钮算起，'候梯(闸机)'从刷闸机算起——两列之差就是找按钮、按按钮花掉的时间；")
    print("  · 加一步改造如果某一列反而变大，说明这一步单独看是有代价的，别只挑好看的数字讲；")
    print("  · '无效停靠'那一列才能看出电梯到底少做了多少无用功。")

    return results


# ============================================================
# 九、主程序
# ============================================================

def main():

    print("=" * 56)
    print("红豆斋宿舍楼电梯群控调度仿真系统")
    print("=" * 56)
    print(f"随机种子数：{REPS}    每人上下车耗时：{T_PAX} s")
    print("正在模拟，请稍等……")

    before_runs = []
    after_runs = []

    for rep in range(REPS):
        seed = SEED + rep

        before_runs.append(Simulation(improved=False, seed=seed).run())
        after_runs.append(Simulation(improved=True, seed=seed).run())

    if ABLATE:
        ablation([SEED + i for i in range(REPS)])

    before = average_results(before_runs)
    after = average_results(after_runs)

    ranged = REPS > 1

    print_result(before, ranged)
    print_result(after, ranged)

    print()
    print("=" * 56)
    print("改造前后变化（正数 = 变好了）")
    print("=" * 56)

    def change(old, new):
        if old == 0:
            return 0.0
        return (old - new) / old * 100

    rows = [
        ("平均候梯（按按钮起算）", "average_wait"),
        ("平均候梯（刷闸机起算）", "average_gate_wait"),
        ("P95 候梯", "p95_wait"),
        ("最长候梯", "max_wait"),
        ("高峰平均候梯", "wait_peak"),
        ("高峰最长候梯", "max_wait_peak"),
        ("开门次数", "open_count"),
        ("开关门动作总数", "door_actions"),
        ("无效停靠", "invalid_stops"),
    ]

    for label, key in rows:
        print(f"{label:<22} {change(before[key], after[key]):+7.2f}%"
              f"   ({before[key]:.2f} -> {after[key]:.2f})")

    print()
    print("平均载客      "
          f"{before['average_load']:.2f} -> {after['average_load']:.2f} 人")
    print("运行利用率    "
          f"{before['utilization']:.2f}% -> {after['utilization']:.2f}%")

    print()
    print("以上只是本模型下的一次实验结果，不代表现实电梯一定能达到。")
    print("注意：本模型的候梯时间不含'走到按钮前'的时间；")
    print("      想看含按键延迟的版本，请用'刷闸机起算'那一栏。")

    save_csv(before, after)
    draw(before, after)


if __name__ == "__main__":
    main()
