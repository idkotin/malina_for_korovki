"""Offline replay; input is a private telemetry export, never commit it.

Run from repository root: python scripts/replay_weight_filter.py EXPORT.json
Columns: st (measurement milliseconds), raw (calibrated kg), sv, sw, cal, packet.
Reported spread is NOT error against known mass, especially during unloading.
"""
import datetime as dt
import json
from pathlib import Path
import statistics
import sys
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from host_monitor.adaptive_weight_filter import AdaptiveWeightFilter
from host_monitor.weight_display import WeightDisplay


def old_step(step):
    q = deque([1000.] * 9, maxlen=9)
    y = 1000.
    for i in range(120):
        q.append(1000+step)
        m = statistics.median(q)
        y += (.35 if abs(m-y) >= 150 else .1) * (m-y)
        if abs(y-1000-step) <= abs(step)*.1:
            return i*.5


def main(path):
    rows = sorted(json.loads(Path(path).read_text()), key=lambda r: (r.get('st') or r['t'], r['id']))
    f, display = AdaptiveWeightFilter(), WeightDisplay()
    seen, output, last_cal = set(), [], None
    for r in rows:
        packet = r.get('packet') or r['id']
        if packet in seen:
            continue
        seen.add(packet)
        if not r.get('sv') or r.get('raw') is None or r.get('cal') != last_cal:
            f.reset()
            display.reset()
        last_cal = r.get('cal')
        if r.get('sv') and r.get('raw') is not None:
            t = (r.get('st') or r['t']) / 1000
            output.append((t, r['sw'], display.update(f.update(r['raw'], t))))
    report = {'step_90_percent_seconds': {}, 'windows': {}}
    for step in (20, 60, 100, 1000, -100):
        f = AdaptiveWeightFilter()
        for i in range(40):
            f.update(1000, i*.5)
        ys = [f.update(1000+step, 20+i*.5) for i in range(120)]
        report['step_90_percent_seconds'][step] = {
            'old': old_step(step),
            'adaptive': next(i*.5 for i,y in enumerate(ys) if abs(y-1000-step) <= abs(step)*.1)}
    for start, end in [('08:19:00', '08:22:00'), ('08:41:00', '08:46:00'), ('08:28:00', '08:38:00')]:
        a,b = [dt.datetime.fromisoformat('2026-09-28T'+v+'+07:00').timestamp() for v in (start,end)]
        window = [r for r in output if a <= r[0] < b]
        report['windows'][start+'-'+end] = {}
        for name, col in [('recorded',1), ('adaptive',2)]:
            values = [r[col] for r in window if r[col] is not None]
            if values:
                report['windows'][start+'-'+end][name] = {
                    'count':len(values), 'min':min(values), 'max':max(values),
                    'sd':round(statistics.stdev(values),2)}
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main(sys.argv[1])
