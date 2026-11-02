REQUIRED_SLOTS = ("panorama", "white_isolated", "twin_bed", "nightstand_closeup")


def quad_version_snapshot(design):
    return {
        slot_type: design["slots"][slot_type]["current_version"]["id"]
        for slot_type in REQUIRED_SLOTS
    }
