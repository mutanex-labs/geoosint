from geoosint.core.models import Bounds, CandidateInput, Point


def test_uncertain_top_three_is_not_verification_and_does_not_fill(runtime):
    service = runtime.service
    for count in (1, 4):
        cid = service.create("a", "Top candidates", f"create-{count}")["id"]
        service.candidates(
            "a",
            cid,
            1,
            f"candidates-{count}",
            [
                CandidateInput(
                    label=f"Hypothesis {index}",
                    level="AREA",
                    area=Bounds(south=0, west=0, north=1, east=1),
                    point=Point(latitude=0.5, longitude=0.5),
                )
                for index in range(count)
            ],
        )
        finalized = service.finalize("a", cid, 2, f"final-{count}")["result"]
        assert finalized["scene_verified"] is False
        top = finalized["top_candidates"]
        assert len(top) == min(3, count)
        assert [candidate["rank"] for candidate in top] == list(range(1, len(top) + 1))
        assert all(
            candidate["qualitative_confidence"] == "LOW"
            and candidate["radius_m"] is None
            and candidate["supporting_reasons"]
            and candidate["verification_gaps"]
            for candidate in top
        )
        assert len(service.read("a", cid)["candidates"]) == count
