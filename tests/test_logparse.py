"""The datalog parser: the acceptance suite, carried over from TunePortal.

The excerpts below are pieces of real files -- trailing comma padding and all --
so this is not a test of the parser against itself. A port is right when the same
excerpts give the same columns, units and values.

Run with:  py tests/test_logparse.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcmess.logenums import describe_value, enum_for  # noqa: E402
from mcmess.logparse import (  # noqa: E402
    LogColumn,
    LogSeries,
    ParsedLog,
    decode_log_bytes,
    default_selection,
    find_rpm_series,
    format_value,
    looks_binary,
    parse_log,
    row_time,
    row_values,
    scale_group_key,
    series_points,
)

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  ok   {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL {label}:\n         got  {got!r}\n         want {want!r}")


# Excerpts of real files, trailing comma padding included.
VCDS_MB = "\r\n".join([
    "Friday,22,August,2025,20:13:30:00009-VCID:72EB6FCF6DAF2803CD-8026,VCDS Version: Release 23.3.1 (x64),Data version: 20230421 DS346.2",
    "06J 906 027 DF,,MED17.5.2        05 2465,",
    "",
    ",Group A:,'115,,,,Group B:,'118,,,,Group C:, Not Running",
    ",,Engine Speed,Engine Load,Boost Pressure,Boost Pressure,,Engine Speed,Intake Air,Boost Pressure,Boost Pressure,,,,,",
    ",TIME,(G28),,(specified),(actual),TIME,(G28),Temperature (G42),Control (N75),(actual),TIME,,,,",
    "Marker,STAMP, /min, %, mbar, mbar,STAMP, /min,°C, %, mbar,STAMP,,,,",
    ",0.01,800,26.3,460.0,950.0,0.09,800,52.0,2.0,950.0,,,,,",
    ",0.17,800,27.8,460.0,950.0,0.25,800,52.0,2.0,950.0,,,,,",
    ",0.33,840,27.8,400.0,950.0,0.40,840,52.0,2.0,950.0,,,,,",
    ",0.47,840,25.6,410.0,960.0,0.54,840,53.0,96.5,955.0,,,,,",
    "",
])

VCDS_ADV = "\r\n".join([
    "Wednesday,16,September,2026,21:46:25:00009-VCID:62CB1F8FBD4FB883BD-8036,VCDS Version: Release 23.3.1 (x64),Data version: 20230421 DS346.2",
    "03H 906 032 AB,ADVMB,P3.2-FSI-EU4    G00 0764,",
    ",,G002,F0,G002,F1,G003,F2,G003,F3,G054,F2,G000,F0,G000,F0,",
    "",
    "Marker,TIME,Group 2 - Field 0,TIME,Group 2 - Field 1,TIME,Group 3 - Field 2,TIME,Group 3 - Field 3,TIME,Group 54 - Field 2,,,,",
    ",STAMP,Engine Speed - (G28),STAMP,Engine Load,STAMP,Throttle Valve Angle,STAMP,Ignition - Timing Angle,STAMP,Accel. Pedal Pos. - Sensor 1 (G79),,,,",
    ",, /min,, %,, %,, °BTDC,, %,,,,",
    ",0.06,600,0.06,22.6,0.11,3.1,0.11,15.0,0.03,0.0,,,,",
    ",0.20,600,0.20,22.6,0.25,3.1,0.25,13.5,0.16,0.0,,,,",
    ",2.64,640,2.64,58.6,2.67,87.5,2.67,3.0,2.57,70.6,,,,",
    ",3.97,1200,3.97,52.6,4.01,7.5,4.01,-15.8,3.92,0.0,,,,",
])

ME7 = "\n".join([
    "###################################################################################,,,,,",
    "Logfile created by ME7-Logger v1.20 (c) mki, 12/2010-07/2013,,,,,",
    ",,,,,",
    "ECU identified with following data:,,,,,",
    "HWNumber    = 0261206892       (requested was 0261206892),,,,,",
    "SWNumber    = 1037360288       (requested was 1037360288),,,,,",
    "Logging with:    30 samples/second,,,,,",
    "Log started at:  28.08.2026 19:18:05.653,,,,,",
    ",,,,,",
    "TimeStamp, fnwue     , nmot_w    , plsol     , pvdks_w   , zwout     ",
    "  sec.ms , -         , rpm       , mbar      , mbar      , °KW       ",
    "TIME,,EngineSpeed,BoostPressureDesired,BoostPressureActual,IgnitionTimingAngleOverall",
    "0,0.0390625,1225,970,968.867,16.5",
    "0.033,0.0390625,1231.75,970,968.867,16.5",
    "0.067,0.0390625,1239.75,970,971.484,17.25",
])

# An excerpt of a real OP-COM log: tag lines, a [SELINFO] row of zeros straight
# above the names, text states in the data, and rpm written "1.501".
OPCOM = "\r\n".join([
    "[PROGVER],OP-COM 100219a - PC based diagnostic tool",
    "[TIMESTAMP],19/09/2026. 12:52",
    "[WSINFO],,,",
    "[MODELINFO],2005 (5),Astra-G,Engine,Z 20 LET",
    "[ECUINFO],Keyword #1: EF,Keyword #2: 8F,Normal timing parameter set.,",
    "[MBLOCKSINFORMATION]",
    "",
    "[SELINFO],0,0,0,4,0,0,8,0,",
    "Time,Battery Voltage,Main Relay,Throttle Position,Engine Speed,Mass Air Flow Sensor,Mass Air Flow Sensor,Knock Control,Boost Pressure Command,Desired Idle Speed,A/C Relay,",
    "sec,V,,,RPM,V,kg/h,,kPa,RPM,,",
    "0.25,14.1,Active,Partial Load,900,1.90,44,Inactive,98,1.062,Not Programmed,",
    "0.61,14.0,Active,Partial Load,998,1.90,44,Inactive,98,1.062,Not Programmed,",
    "0.94,14.1,Active,Partial Load,1.501,1.88,45,Active,125,1.062,Not Programmed,",
    "1.28,14.1,Active,Idle,6.082,4.63,262,Active,183,1.062,Not Programmed,",
])

#: What this project's own recorder writes: five "#" provenance lines, then a
#: names row carrying "Name [unit]", then the grid. "t_s" is its clock.
OURS = "\n".join([
    "# McMESS log, Bosch M3.8 / 06A906018CG",
    "# recorded 2026-11-19 12:56:40",
    "# link COM2 at 125000 baud (highspeed), cable echo yes",
    "# sample costs 25 wire bytes",
    "# conversions from DAMOS 8D0907558E (Audi A4 M3.8.2) - cross-image inference",
    "t_s,ML [kg/h],ZWOUT [grad KW],N40 [Upm],TL [ms/Umdr.],KLINFO [dez],KNK_CYL1",
    "43.3386,35.25,28.5,1280,2.35,2,0",
    "43.4150,36.00,28.5,1281,2.36,130,1",
    "43.4914,36.75,27.0,1290,2.40,1,0",
])


def main() -> int:
    print("VCDS measuring blocks: two name rows, a units row, one time column per group")
    log = parse_log(VCDS_MB)
    check("format", log.format, "vcds")
    check("four data rows", len(log.rows), 4)
    check("labels", [s.label for s in log.series], [
        "Engine Speed (G28) [115]", "Engine Load", "Boost Pressure (specified)",
        "Boost Pressure (actual) [115]", "Engine Speed (G28) [118]",
        "Intake Air Temperature (G42)", "Boost Pressure Control (N75)",
        "Boost Pressure (actual) [118]",
    ])
    check("units", [s.unit for s in log.series],
          ["/min", "%", "mbar", "mbar", "/min", "°C", "%", "mbar"])
    a, b = log.series[0], log.series[4]
    check("group B rides on group B's clock", a.time_col != b.time_col, True)
    check("group A stamps", series_points(log, a)[0], [0.01, 0.17, 0.33, 0.47])
    check("group B stamps", series_points(log, b)[0], [0.09, 0.25, 0.40, 0.54])
    check("group B values", series_points(log, b)[1], [800, 800, 840, 840])
    check("meta carries the part number",
          any("06J 906 027 DF" in m and "MED17.5.2" in m for m in log.meta), True)
    check("padding columns are dropped", all(c.label for c in log.columns), True)

    print("\nVCDS advanced: a time stamp in front of every field")
    log = parse_log(VCDS_ADV)
    check("format", log.format, "vcds-advanced")
    check("labels", [s.label for s in log.series], [
        "Engine Speed - (G28)", "Engine Load", "Throttle Valve Angle",
        "Ignition - Timing Angle", "Accel. Pedal Pos. - Sensor 1 (G79)",
    ])
    check("units", [s.unit for s in log.series],
          ["/min", "%", "%", "°BTDC", "%"])
    timing = log.series[3]
    check("negative values parse", series_points(log, timing)[1], [15.0, 13.5, 3.0, -15.8])
    check("its own stamps", series_points(log, timing)[0], [0.11, 0.25, 2.67, 4.01])
    check("five fields, five clocks", len({s.time_col for s in log.series}), 5)

    print("\nME7Logger: variable names, units, aliases; one time column")
    log = parse_log(ME7)
    check("format", log.format, "me7logger")
    check("labels", [s.label for s in log.series], [
        "fnwue", "EngineSpeed (nmot_w)", "BoostPressureDesired (plsol)",
        "BoostPressureActual (pvdks_w)", "IgnitionTimingAngleOverall (zwout)",
    ])
    check("units", [s.unit for s in log.series], ["", "rpm", "mbar", "mbar", "°KW"])
    check("one clock for all", all(s.time_col == 0 for s in log.series), True)
    check("fnwue never moves", log.series[0].constant, True)
    check("min", log.series[1].min, 1225)
    check("max", log.series[1].max, 1239.75)
    check("meta: the = line becomes a : line", "HWNumber: 0261206892" in log.meta, True)
    check("meta: the start time survives",
          any(m.startswith("Log started at") for m in log.meta), True)

    print("\nGeneric CSV: semicolons with decimal commas, units in brackets")
    log = parse_log("\n".join([
        "Time (s);RPM;Boost (bar);AFR",
        "0,0;900;0,15;14,7", "0,1;950;0,18;14,6", "0,2;1000;0,22;14,2",
    ]))
    check("format", log.format, "csv")
    check("names and units", [(s.label, s.unit) for s in log.series],
          [("RPM", ""), ("Boost", "bar"), ("AFR", "")])
    check("decimal commas", series_points(log, log.series[1]),
          ([0.0, 0.1, 0.2], [0.15, 0.18, 0.22], [0, 1, 2]))

    print("\nGeneric CSV with no time column")
    rising = parse_log("\n".join(
        ["sample,boost,rpm"]
        + [f"{i * 5},{1000 + i},{2000 + i * 100}" for i in range(12)]
    ))
    check("a rising first column is the clock", rising.columns[0].is_time, True)
    none = parse_log("\n".join(["boost,rpm", "1000,2500", "1100,2400", "1200,2300"]))
    check("otherwise there is no clock", none.series[0].time_col, None)
    check("and x is the row number", series_points(none, none.series[0])[0], [0.0, 1.0, 2.0])

    print("\nPlotting against another channel uses that channel as x")
    log = parse_log(ME7)
    rpm, boost = log.series[1], log.series[3]
    check("boost against rpm", series_points(log, boost, rpm.col)[0],
          [1225, 1231.75, 1239.75])

    print("\nThe default selection prefers what a tuner looks at first")
    log = parse_log(ME7)
    by_key = log.by_key
    labels = [by_key[k].label for k in default_selection(log)]
    check("engine speed first", labels[0].startswith("EngineSpeed"), True)
    check("the flat channel is skipped", "fnwue" in labels, False)

    print("\nText that is not a log, and files that are not text, are refused")
    check("prose", parse_log("hello\nworld"), None)
    check("nothing", parse_log(""), None)
    check("zip / xlsx", looks_binary(b"PK\x03\x04\x00\x00\x00"), True)
    check("a csv is not binary", looks_binary(b"a,b\n1,2\n"), False)

    print("\nVCDS bytes are Windows-1252: the degree sign survives")
    check("cp1252 fallback", decode_log_bytes(b"\xb0\x43"), "°C")
    check("UTF-8 stays UTF-8", decode_log_bytes("°C".encode("utf-8")), "°C")

    print("\nOP-COM: rows that are half text are data, the [SELINFO] zeros are not")
    log = parse_log(OPCOM)
    check("format", log.format, "opcom")
    check("four data rows", len(log.rows), 4)
    check("the first column is the clock", log.columns[0].is_time, True)
    check("names and units", [(s.label, s.unit) for s in log.series], [
        ("Battery Voltage", "V"), ("Main Relay", "0/1"), ("Engine Speed", "RPM"),
        ("Mass Air Flow Sensor [V]", "V"), ("Mass Air Flow Sensor [kg/h]", "kg/h"),
        ("Knock Control", "0/1"), ("Boost Pressure Command", "kPa"),
        ("Desired Idle Speed", "RPM"),
    ])
    check("the model line is in the meta",
          "2005 (5) · Astra-G · Engine · Z 20 LET" in log.meta, True)
    check("the tool line leads", log.meta[0].startswith("OP-COM 100219a"), True)

    print("\nOP-COM: rpm over 999 is written \"1.501\" and reads as 1501")
    rpm = next(s for s in log.series if s.label == "Engine Speed")
    check("repaired", series_points(log, rpm)[1], [900, 998, 1501, 6082])
    check("and the max with it", rpm.max, 6082)
    idle = next(s for s in log.series if s.label == "Desired Idle Speed")
    check("the idle target too", series_points(log, idle)[1], [1062, 1062, 1062, 1062])
    maf = next(s for s in log.series if s.label == "Mass Air Flow Sensor [V]")
    check("1.90 V stays 1.90 V", series_points(log, maf)[1], [1.90, 1.90, 1.88, 4.63])

    print("\nOP-COM: a two-state signal becomes 0/1; other text is left out")
    knock = next(s for s in log.series if s.label == "Knock Control")
    check("Inactive/Active -> 0/1", series_points(log, knock)[1], [0, 0, 1, 1])
    check("\"Partial Load / Idle\" is not two-state",
          any(s.label == "Throttle Position" for s in log.series), False)
    check("\"Not Programmed\" is not either",
          any(s.label == "A/C Relay" for s in log.series), False)

    print("\nThe rpm repair is OP-COM only, and only when every value fits")
    plain = parse_log("\n".join(
        ["Time,Engine Speed,Lambda", "sec,RPM,", "0.1,1.501,0.98", "0.2,1.498,0.97"]
    ))
    check("not an OP-COM file: left alone",
          series_points(plain, plain.series[0])[1], [1.501, 1.498])
    odd = parse_log("\n".join([
        "[PROGVER],OP-COM x", "Time,Engine Speed", "sec,RPM",
        "0.1,1.5", "0.2,1.501", "0.3,2.25",
    ]))
    check("values that are not d.ddd: not plausible, left alone",
          series_points(odd, odd.series[0])[1], [1.5, 1.501, 2.25])

    print("\nThe second x axis finds engine speed by name, else by unit")
    check("ME7Logger", find_rpm_series(parse_log(ME7)).label, "EngineSpeed (nmot_w)")
    check("VCDS", find_rpm_series(parse_log(VCDS_MB)).label, "Engine Speed (G28) [115]")
    check("OP-COM", find_rpm_series(parse_log(OPCOM)).label, "Engine Speed")
    by_unit = parse_log("\n".join([
        "Time,Drehzahl,Ladedruck", "sec,1/min,mbar",
        "0.1,900,1000", "0.2,1500,1100", "0.3,2100,1300",
    ]))
    check("by unit", find_rpm_series(by_unit).label, "Drehzahl")
    check("a log with no speed channel",
          find_rpm_series(parse_log("\n".join(
              ["Time,Boost", "0.1,1000", "0.2,1100", "0.3,1200"]))), None)

    print("\nrow_values reads every channel on a row, row_time its stamp")
    log = parse_log(VCDS_MB)
    first = row_values(log, 0)
    check("every channel has a value on row 0", len(first), len(log.series))
    check("and it is the grid's",
          all(first[s.key] == log.rows[0][s.col] for s in log.series), True)
    past = row_values(log, 999)
    check("a row past the end is the last row",
          all(past[s.key] == log.rows[-1][s.col] for s in log.series), True)
    time_col = next(i for i, c in enumerate(log.columns) if c.is_time)
    check("row_time", row_time(log, 0), log.rows[0][time_col])

    print("\nrow_values: a ragged channel takes its nearest sample")
    def ch(key: str, col: int) -> LogSeries:
        return LogSeries(key=key, label=key, unit="", col=col, time_col=0,
                         min=0, max=1, constant=False, count=2)

    ragged = ParsedLog(
        format="csv", meta=[],
        columns=[LogColumn("Time", "s", True), LogColumn("rpm", "", False),
                 LogColumn("boost", "", False)],
        rows=[[0, 800, None], [0.1, None, 1000], [0.2, 900, 1010]],
        series=[ch("c1", 1), ch("c2", 2)],
    )
    check("boost has no sample on row 0: the next row",
          row_values(ragged, 0), {"c1": 800, "c2": 1000})
    check("rpm has none on row 1: the earlier neighbour on a tie",
          row_values(ragged, 1), {"c1": 800, "c2": 1000})
    check("row 2 is complete", row_values(ragged, 2), {"c1": 900, "c2": 1010})
    check("row_time", row_time(ragged, 2), 0.2)
    check("series_points says which row each point is",
          series_points(ragged, ragged.series[0])[2], [0, 2])
    check("and for the ragged one",
          series_points(ragged, ragged.series[1])[2], [1, 2])
    empty = ParsedLog(format="csv", columns=ragged.columns, rows=[], series=ragged.series)
    check("an empty log has nothing to read", row_values(empty, 0), {})

    print("\nOur own logs: the # provenance lines are meta, not column names")
    log = parse_log(OURS)
    check("format", log.format, "csv")
    check("t_s is the clock", log.columns[0].is_time, True)
    check("names and units", [(s.label, s.unit) for s in log.series], [
        ("ML", "kg/h"), ("ZWOUT", "grad KW"), ("N40", "Upm"),
        ("TL", "ms/Umdr."), ("KLINFO", "dez"), ("KNK_CYL1", ""),
    ])
    check("the ECU is named in the meta",
          any("06A906018CG" in m for m in log.meta), True)
    check("so is the DAMOS caveat",
          any("cross-image inference" in m for m in log.meta), True)
    check("no leading hash survives", any(m.startswith("#") for m in log.meta), False)
    check("three rows", len(log.rows), 3)
    check("rpm is found", find_rpm_series(log).label, "N40")

    print("\nScale groups and value formatting")
    s_mbar = ch("c1", 1)
    s_mbar.unit = "mbar"
    check("a unit is shared", scale_group_key(s_mbar), "u:mbar")
    check("own scale on request", scale_group_key(s_mbar, True), "k:c1")
    check("no unit, no sharing", scale_group_key(ch("c9", 9)), "k:c9")
    check("0 decimals from 1000", format_value(3040.0), "3040")
    check("1 from 100", format_value(140.55), "140.6")
    check("2 from 1", format_value(1.50), "1.5")
    check("3 below 1", format_value(0.1234), "0.123")
    check("negative zero collapses", format_value(-0.0), "0")
    check("nothing is --", format_value(None), "--")

    print("\nCode tables: the channels that log a code, not a quantity")
    check("KLINFO is packed", enum_for("KLINFO [dez]").kind, "packed")
    check("a plain quantity is not", enum_for("Engine Speed"), None)
    check("nor is boost", enum_for("Boost (mbar)"), None)
    klinfo = enum_for("KLINFO")
    check("2 is cylinder 2, no knock", describe_value(klinfo, 2),
          "cylinder 2 (raw), no knock")
    check("130 is cylinder 2 with the bit-7 flag", describe_value(klinfo, 130),
          "cylinder 2 (raw), knock detected")
    check("a bogus value says so", describe_value(klinfo, 9), "raw 9: outside 0..4")
    check("KNK_CYL reads as words", describe_value(enum_for("KNK_CYL3"), 1.0),
          "knock detected")

    print("\nCode tables: BMW MG1 as MHD logs it")
    check("RF Max Index", enum_for("RF Max Index (-)").kind, "value")
    check("TQ Max Index", enum_for("TQ Max Index (-)").kind, "value")
    check("Status Torque limiter is a mask", enum_for("Status Torque limiter (-)").kind,
          "flags")
    check("so is Torque lim. 1", enum_for("Torque lim. 1 (-)").kind, "flags")
    fill = enum_for("RF Max Index (-)")
    check("0", describe_value(fill, 0),
          "Standard limitation: fill from the torque request only")
    check("2", describe_value(fill, 2),
          "Turbo component protection: compressor pressure ratio")
    check("5", describe_value(fill, 5),
          "Intake temperature, knock or driving-behaviour retard")
    check("an unknown code shows the raw number alone", describe_value(fill, 42), None)
    mask = enum_for("Status Torque limiter (-)")
    check("none", describe_value(mask, 0), "none")
    check("2048 is the rev limiter", describe_value(mask, 2048), "NMAX: rev limiter")
    check("262144 is engine protection", describe_value(mask, 262144),
          "MD_MOTOR: engine protection")
    check("3 is two bits at once", describe_value(mask, 3),
          "ASR: traction control, torque reduction + "
          "MSR: engine drag control, torque increase")
    check("4 + 131072", describe_value(mask, 4 + 131072),
          "GS_DEC: gearbox torque decrease (upshift) + "
          "MD_RF: cylinder fill limit (see RF Max Index)")
    index = enum_for("TQ Max Index (-)")
    check("11 is the bit number, not a mask", describe_value(index, 11),
          "NMAX: rev limiter")
    check("18 likewise", describe_value(index, 18), "MD_MOTOR: engine protection")

    print("\nA real log off the car, if one is lying about")
    real = Path(__file__).resolve().parent.parent / "logs" / "432.csv"
    if real.exists():
        from mcmess.logparse import read_log

        log = read_log(real)
        check("it opens", log is not None, True)
        check("its clock is t_s", log.columns[0].is_time, True)
        check("every channel is named", all(c.label for c in log.columns), True)
        check("the pull is in there", len(log.rows) > 100, True)
        rpm = find_rpm_series(log)
        check("rpm found", rpm.label if rpm else None, "N40")
        picked = [log.by_key[k].label for k in default_selection(log)]
        check("the default selection is plausible", len(picked) >= 2, True)
        print(f"       {len(log.rows)} rows, {len(log.series)} channels, "
              f"suggested {', '.join(picked)}")
    else:
        print("  --   logs/432.csv is not here, skipped")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S): {', '.join(FAILURES)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
