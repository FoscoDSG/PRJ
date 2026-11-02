"""Print the provisional hold-out score distribution. Writes nothing."""

from __future__ import annotations

from services.mapping.holdout import holdout_report


def main() -> None:
    report = holdout_report()
    print(f"etichetta: {report['label']}")
    print(f"calibrated: {str(report['calibrated']).lower()}")
    print(f"soglia: {report['threshold_status']} (ripiego {report['threshold']:.2f})")
    print(report["reason"])
    before = report["before_token"]
    after = report["after_token"]
    print(
        f"prima misura, prima di mapping.token@v1: "
        f"{before['above']} sopra, {before['below']} sotto"
    )
    print(
        f"seconda misura, dopo mapping.token@v1: "
        f"{after['above']} sopra, {after['below']} sotto"
    )
    print(after["note"])
    print(
        f"dato sulle sole intestazioni: {report['above']} sopra, {report['below']} sotto"
    )
    print(report["shape_recognition_note"])
    for row in report["columns"]:
        side = "sopra" if row["above_threshold"] else "sotto"
        print(
            f"{row['source']}\t{row['header']}\t{row['profile']}\t"
            f"{row['target_type']}.{row['target_property']}\t"
            f"{row['score']:.2f}\t{row['rule']}\t{side}"
        )


if __name__ == "__main__":
    main()
