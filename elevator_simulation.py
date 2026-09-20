# -*- coding: utf-8 -*-
"""
宿舍楼三电梯仿真

运行：
    python elevator_simulation_simple.py

需要画图时安装：
    pip install matplotlib

程序会比较：
    1. 改造前
    2. 改造后

主要指标：
    平均侯梯时间、P95侯梯时间、最大侯梯时间、
    平均乘梯时间、开门次数、无效停靠次数、
    平均载客人数、电梯利用率
"""

import csv
import math
import random
from dataclasses import dataclass, field


# ============================================================
# 一、这里是你可以自己修改的参数
# ============================================================

FLOORS = 15
FLOOR_HEIGHT = 3.5

ACCELERATION = 0.6       # m/s²
MAX_SPEED = 1.75         # m/s

CAPACITY = 11

DOOR_OPEN = 2.6          # 秒
DOOR_CLOSE = 2.6         # 秒

TOTAL_TIME = 4 * 3600    # 4小时
PEAK_START = 3 * 3600    # 第3小时结束后进入高峰
DRAIN_TIME = 20 * 60     # 4小时结束后，再运行20分钟清空队列

JUNIOR_DELAY = 5         # 改造前学弟刷闸机后5秒才能呼梯

SENIORS_PER_FLOOR = 20
JUNIORS_PER_FLOOR = 80

DT = 0.2
SEED = 42


# ============================================================
# 二、电梯运行时间
# ============================================================

def travel_time(a, b):
    """计算电梯从 a 楼到 b 楼需要多少秒。"""

    distance = abs(a - b) * FLOOR_HEIGHT

    if distance == 0:
        return 0

    # 加速到最高速度再减速，需要的总距离
    accel_decel_distance = MAX_SPEED ** 2 / ACCELERATION

    # 距离短：达不到最高速度
    if distance <= accel_decel_distance:
        return 2 * math.sqrt(distance / ACCELERATION)

    # 距离长：加速 + 匀速 + 减速
    t_acc = MAX_SPEED / ACCELERATION
    cruise_distance = distance - accel_decel_distance
    t_cruise = cruise_distance / MAX_SPEED

    return 2 * t_acc + t_cruise


# ============================================================
# 三、学生和电梯
# ============================================================

@dataclass
class Group:
    """
    一个学生或者一群结伴学生。

    学长：size=1
    学弟：size=2~5
    """

    id: int
    kind: str
    size: int
    origin: int
    destination: int

    gate_time: float
    call_time: float

    board_time: float = None
    arrive_time: float = None
    elevator_id: int = None


@dataclass
class Elevator:
    id: int

    floor: int = 1
    direction: int = 0       # 1向上，-1向下，0空闲
    target: int = None

    state: str = "idle"      # idle/moving/open/close
    timer: float = 0

    onboard: list = field(default_factory=list)
    requests: set = field(default_factory=set)

    open_count: int = 0
    close_count: int = 0
    invalid_stops: int = 0
    moving_time: float = 0

    # 电梯每次离开楼层时的载客人数
    departure_loads: list = field(default_factory=list)
    silent_move: bool = False


# ============================================================
# 四、仿真
# ============================================================

class Simulation:

    def __init__(self, improved=False, seed=SEED):
        self.improved = improved
        self.random = random.Random(seed)

        self.time = 0
        self.groups = {}
        self.order = []

        self.elevators = [
            Elevator(1),
            Elevator(2),
            Elevator(3),
        ]

        self.generate_people()

    # --------------------------------------------------------
    # 电梯服务范围
    # --------------------------------------------------------

    def can_serve(self, elevator, group):
        """
        1号：1楼 + 单数层
        2号：1楼 + 双数层
        3号：全部楼层
        """

        if elevator.id == 1:
            floors = {1, 3, 5, 7, 9, 11, 13, 15}
        elif elevator.id == 2:
            floors = {1, 2, 4, 6, 8, 10, 12, 14}
        else:
            floors = set(range(1, 16))

        return (
            group.origin in floors
            and group.destination in floors
        )

    # --------------------------------------------------------
    # 产生学生
    # --------------------------------------------------------

    def generate_people(self):
        gid = 1

        # 学长：
        # 2~8楼，每层20人，4小时内均匀出现
        for floor in range(2, 9):

            for _ in range(SENIORS_PER_FLOOR):
                t = self.random.uniform(0, TOTAL_TIME)

                group = Group(
                    id=gid,
                    kind="senior",
                    size=1,
                    origin=floor,
                    destination=1,
                    gate_time=t,
                    call_time=t
                )

                self.groups[gid] = group
                gid += 1

        # 学弟：
        # 9~15楼，每层80人。
        # 2~5人结伴，同组同楼层。
        for floor in range(9, 16):

            remaining = JUNIORS_PER_FLOOR

            while remaining > 0:

                size = self.random.randint(2, 5)
                size = min(size, remaining)

                t = self.random.uniform(
                    PEAK_START,
                    TOTAL_TIME
                )

                if self.improved:
                    call = t
                else:
                    call = t + JUNIOR_DELAY

                group = Group(
                    id=gid,
                    kind="junior",
                    size=size,
                    origin=1,
                    destination=floor,
                    gate_time=t,
                    call_time=call
                )

                self.groups[gid] = group
                gid += 1
                remaining -= size

        self.order = sorted(
            self.groups,
            key=lambda x: self.groups[x].call_time
        )

    # --------------------------------------------------------
    # 改造后：选择最合适的电梯
    # --------------------------------------------------------

    def score(self, elevator, group):
        """
        不用AI，只做一个很容易解释的评分：

        距离越近 -> 分数越低
        已有任务越少 -> 分数越低
        当前人数越少 -> 分数越低
        方向相反 -> 加一点分
        """

        score = travel_time(
            elevator.floor,
            group.origin
        )

        score += len(self.get_stops(elevator)) * 4

        load = sum(g.size for g in elevator.onboard)
        score += load * 0.8

        if elevator.direction != 0:
            desired = (
                1 if group.origin > elevator.floor
                else -1
            )

            if desired != elevator.direction:
                score += 8

        return score

    def add_request(self, group):

        candidates = [
            e for e in self.elevators
            if self.can_serve(e, group)
        ]

        if not candidates:
            return

        if self.improved:
            best = min(
                candidates,
                key=lambda e: self.score(e, group)
            )

            best.requests.add(group.id)

        else:
            # 改造前：
            # 相当于同时按下所有可以服务的电梯按钮
            for e in candidates:
                e.requests.add(group.id)

    # --------------------------------------------------------
    # 当前电梯需要停哪些楼层
    # --------------------------------------------------------

    def get_stops(self, elevator):
        stops = set()

        for gid in elevator.requests:

            g = self.groups[gid]

            # 已经由这台电梯接走
            if g.elevator_id == elevator.id:
                if g.arrive_time is None:
                    stops.add(g.destination)

            # 还没人接走
            elif g.elevator_id is None:
                stops.add(g.origin)

            # 改造前的“旧请求”
            # 学生已经被别的电梯接走，
            # 但本电梯仍保留原来的呼梯命令。
            elif not self.improved:
                stops.add(g.origin)

        return stops

    # --------------------------------------------------------
    # LOOK：同向优先
    # --------------------------------------------------------

    def choose_next(self, elevator):

        stops = self.get_stops(elevator)

        if not stops:
            elevator.target = None
            elevator.direction = 0
            elevator.state = "idle"
            return

        current = elevator.floor

        above = sorted(
            x for x in stops
            if x > current
        )

        below = sorted(
            (x for x in stops if x < current),
            reverse=True
        )

        if elevator.direction == 1 and above:
            target = above[0]
            elevator.direction = 1

        elif elevator.direction == -1 and below:
            target = below[0]
            elevator.direction = -1

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

        elevator.state = "moving"

        elevator.timer = travel_time(
            current,
            target
        )

        load = sum(
            g.size for g in elevator.onboard
        )

        elevator.departure_loads.append(load)

    # --------------------------------------------------------
    # 到达一个楼层
    # --------------------------------------------------------

    def stop(self, elevator):

        floor = elevator.floor

        # --------------------------
        # 1. 下客
        # --------------------------

        dropped = []

        for g in elevator.onboard:

            if g.destination == floor:
                g.arrive_time = self.time
                dropped.append(g)

        elevator.onboard = [
            g for g in elevator.onboard
            if g not in dropped
        ]

        # --------------------------
        # 2. 有没有真正等待的人
        # --------------------------

        waiting = False

        for gid in elevator.requests:

            g = self.groups[gid]

            if (
                g.origin == floor
                and g.call_time <= self.time
                and g.elevator_id is None
            ):
                waiting = True
                break

        # --------------------------
        # 3. 判断无效停靠
        # --------------------------

        load = sum(
            g.size for g in elevator.onboard
        )

        invalid = False

        # 没有人下，也没有人等
        if not dropped and not waiting:
            invalid = True

        # 已经满载，而且没人下，
        # 但还有人在等
        if (
            load >= CAPACITY
            and not dropped
            and waiting
        ):
            invalid = True

        if invalid:
            elevator.invalid_stops += 1

        # --------------------------
        # 4. 开门
        # --------------------------

        elevator.open_count += 1
        elevator.state = "open"
        elevator.timer = DOOR_OPEN

        self.board(elevator)

        elevator.target = None

    # --------------------------------------------------------
    # 上客
    # --------------------------------------------------------

    def board(self, elevator):

        floor = elevator.floor

        load = sum(
            g.size for g in elevator.onboard
        )

        left = CAPACITY - load

        candidates = []

        for gid in elevator.requests:

            g = self.groups[gid]

            if (
                g.origin == floor
                and g.call_time <= self.time
                and g.elevator_id is None
            ):
                candidates.append(g)

        candidates.sort(
            key=lambda g: g.call_time
        )

        for g in candidates:

            if g.size <= left:

                g.board_time = self.time
                g.elevator_id = elevator.id

                elevator.onboard.append(g)

                left -= g.size

            if left <= 0:
                break

    # --------------------------------------------------------
    # 简单预调度
    # --------------------------------------------------------

    def pre_dispatch(self):

        if not self.improved:
            return

        if self.time < PEAK_START - 60:
            return

        for e in self.elevators:

            if (
                e.state == "idle"
                and not e.requests
                and e.floor != 1
            ):
                e.target = 1
                e.direction = -1
                e.state = "moving"

                e.timer = travel_time(
                    e.floor,
                    1
                )

                e.departure_loads.append(0)

    # --------------------------------------------------------
    # 主循环
    # --------------------------------------------------------

    def run(self):

        index = 0

        ordered = [
            self.groups[gid]
            for gid in self.order
        ]

        while self.time <= TOTAL_TIME + DRAIN_TIME:

            # 新的呼梯请求
            while (
                index < len(ordered)
                and ordered[index].call_time <= self.time
            ):
                self.add_request(ordered[index])
                index += 1

            self.pre_dispatch()

            for e in self.elevators:

                # 电梯运行
                if e.state == "moving":

                    e.timer -= DT
                    e.moving_time += DT

                    if e.timer <= 0:
                        e.floor = e.target

                        # 预调度回1楼只是“提前把电梯放过去”，
                        # 不是一次载客停靠，所以不开门，也不计入无效停靠。
                        if e.silent_move:
                            e.target = None
                            e.direction = 0
                            e.state = "idle"
                            e.silent_move = False
                        else:
                            self.stop(e)

                # 开门
                elif e.state == "open":

                    e.timer -= DT

                    if e.timer <= 0:
                        e.state = "close"
                        e.timer = DOOR_CLOSE
                        e.close_count += 1

                # 关门
                elif e.state == "close":

                    e.timer -= DT

                    if e.timer <= 0:
                        e.state = "idle"
                        self.choose_next(e)

                # 空闲
                elif e.state == "idle":

                    self.choose_next(e)

            self.time += DT

        return self.metrics()

    # --------------------------------------------------------
    # P95
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

        return (
            values[low]
            + (values[high] - values[low])
            * (index - low)
        )

    # --------------------------------------------------------
    # 统计
    # --------------------------------------------------------

    def metrics(self):

        waits = []
        rides = []

        total_people = 0
        completed_people = 0

        for g in self.groups.values():

            total_people += g.size

            if g.board_time is not None:

                wait = g.board_time - g.call_time

                for _ in range(g.size):
                    waits.append(wait)

            if (
                g.board_time is not None
                and g.arrive_time is not None
            ):

                completed_people += g.size

                ride = (
                    g.arrive_time
                    - g.board_time
                )

                for _ in range(g.size):
                    rides.append(ride)

        opens = sum(
            e.open_count
            for e in self.elevators
        )

        closes = sum(
            e.close_count
            for e in self.elevators
        )

        invalid = sum(
            e.invalid_stops
            for e in self.elevators
        )

        moving = sum(
            e.moving_time
            for e in self.elevators
        )

        loads = []

        for e in self.elevators:
            loads.extend(
                x for x in e.departure_loads
                if x > 0
            )

        average_load = (
            sum(loads) / len(loads)
            if loads else 0
        )

        utilization = (
            moving
            / (TOTAL_TIME * 3)
            * 100
        )

        return {
            "scenario": (
                "改造后"
                if self.improved
                else "改造前"
            ),
            "total_people": total_people,
            "completed_people": completed_people,
            "completion_rate": (
                completed_people
                / total_people
                * 100
            ),
            "average_wait": (
                sum(waits) / len(waits)
                if waits else 0
            ),
            "p95_wait": self.percentile(
                waits,
                0.95
            ),
            "max_wait": (
                max(waits)
                if waits else 0
            ),
            "average_ride": (
                sum(rides) / len(rides)
                if rides else 0
            ),
            "open_count": opens,
            "close_count": closes,
            "door_actions": opens + closes,
            "invalid_stops": invalid,
            "average_load": average_load,
            "utilization": utilization,
        }


# ============================================================
# 五、打印结果
# ============================================================

def print_result(result):

    print()
    print("=" * 50)
    print(result["scenario"])
    print("=" * 50)

    print(
        f"总学生人数：{result['total_people']}"
    )

    print(
        f"完成运送人数：{result['completed_people']}"
    )

    print(
        f"完成率：{result['completion_rate']:.2f}%"
    )

    print(
        f"平均侯梯时间：{result['average_wait']:.2f} s"
    )

    print(
        f"P95侯梯时间：{result['p95_wait']:.2f} s"
    )

    print(
        f"最大侯梯时间：{result['max_wait']:.2f} s"
    )

    print(
        f"平均乘梯时间：{result['average_ride']:.2f} s"
    )

    print(
        f"开门次数：{result['open_count']}"
    )

    print(
        f"关门次数：{result['close_count']}"
    )

    print(
        f"开关门动作总数：{result['door_actions']}"
    )

    print(
        f"无效停靠次数：{result['invalid_stops']}"
    )

    print(
        f"平均每次出发载客人数："
        f"{result['average_load']:.2f} 人"
    )

    print(
        f"电梯运行利用率："
        f"{result['utilization']:.2f}%"
    )


# ============================================================
# 六、保存CSV
# ============================================================

def save_csv(before, after):

    filename = "simulation_results.csv"

    fields = [
        "scenario",
        "total_people",
        "completed_people",
        "completion_rate",
        "average_wait",
        "p95_wait",
        "max_wait",
        "average_ride",
        "open_count",
        "close_count",
        "door_actions",
        "invalid_stops",
        "average_load",
        "utilization",
    ]

    with open(
        filename,
        "w",
        newline="",
        encoding="utf-8-sig"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=fields
        )

        writer.writeheader()
        writer.writerow(before)
        writer.writerow(after)

    print()
    print(f"结果已经保存到：{filename}")


# ============================================================
# 七、简单画图
# ============================================================

def draw(before, after):

    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print()
        print("没有安装 matplotlib，跳过画图。")
        print("需要的话运行：pip install matplotlib")
        return

    names = [
        "Average wait",
        "P95 wait",
        "Max wait"
    ]

    old = [
        before["average_wait"],
        before["p95_wait"],
        before["max_wait"]
    ]

    new = [
        after["average_wait"],
        after["p95_wait"],
        after["max_wait"]
    ]

    x = range(len(names))
    width = 0.35

    plt.figure(figsize=(9, 5))

    plt.bar(
        [i - width / 2 for i in x],
        old,
        width,
        label="Before"
    )

    plt.bar(
        [i + width / 2 for i in x],
        new,
        width,
        label="After"
    )

    plt.xticks(list(x), names)
    plt.ylabel("seconds")
    plt.title("Waiting Time")
    plt.legend()
    plt.tight_layout()
    plt.savefig(
        "waiting_time_comparison.png",
        dpi=160
    )

    names = [
        "Door opens",
        "Invalid stops",
        "Average load"
    ]

    old = [
        before["open_count"],
        before["invalid_stops"],
        before["average_load"]
    ]

    new = [
        after["open_count"],
        after["invalid_stops"],
        after["average_load"]
    ]

    plt.figure(figsize=(9, 5))

    plt.bar(
        [i - width / 2 for i in range(3)],
        old,
        width,
        label="Before"
    )

    plt.bar(
        [i + width / 2 for i in range(3)],
        new,
        width,
        label="After"
    )

    plt.xticks(
        range(3),
        names
    )

    plt.ylabel("value")
    plt.title("Operation Comparison")
    plt.legend()
    plt.tight_layout()
    plt.savefig(
        "operation_comparison.png",
        dpi=160
    )

    print("图表已经保存。")


# ============================================================
# 八、主程序
# ============================================================

def main():

    print("=" * 50)
    print("宿舍楼三电梯调度仿真")
    print("=" * 50)

    print("正在模拟，请稍等……")

    # 最重要的一点：
    # 两次模拟使用相同随机种子。
    #
    # 所以改造前和改造后面对的是
    # 同一批学生、同一批客流。
    #
    # 这样比较才公平。

    before_sim = Simulation(
        improved=False,
        seed=SEED
    )

    before = before_sim.run()

    after_sim = Simulation(
        improved=True,
        seed=SEED
    )

    after = after_sim.run()

    print_result(before)
    print_result(after)

    print()
    print("=" * 50)
    print("改造前后变化")
    print("=" * 50)

    def change(old, new):
        if old == 0:
            return 0
        return (old - new) / old * 100

    print(
        f"平均侯梯时间变化："
        f"{change(before['average_wait'], after['average_wait']):.2f}%"
    )

    print(
        f"P95侯梯时间变化："
        f"{change(before['p95_wait'], after['p95_wait']):.2f}%"
    )

    print(
        f"最大侯梯时间变化："
        f"{change(before['max_wait'], after['max_wait']):.2f}%"
    )

    print(
        f"开门次数变化："
        f"{change(before['open_count'], after['open_count']):.2f}%"
    )

    print(
        f"无效停靠变化："
        f"{change(before['invalid_stops'], after['invalid_stops']):.2f}%"
    )

    print()
    print("以上只是本模型、当前随机种子下的一次实验结果。")
    print("不要直接把这些数字当成现实电梯一定能达到的效果。")

    save_csv(before, after)
    draw(before, after)


if __name__ == "__main__":
    main()
