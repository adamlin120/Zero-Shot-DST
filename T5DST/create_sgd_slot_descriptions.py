import json

from datasets import load_dataset


def main():
    slot_desc = {}
    for dataset in load_dataset("schema_guided_dstc8", "schema").values():
        for service in dataset:
            for i in range(len(service["slots"]["is_categorical"])):
                name = service["slots"]["name"][i]
                description_human = service["slots"]["description"][i]
                values = service["slots"]["possible_values"][i]
                slot_desc[f"{service['service_name']}-{name}"] = {
                    "description_human": description_human,
                    "values": values,
                }

    with open("./utils/slot_description.json", "w") as f:
        json.dump(slot_desc, f, indent=2)


if __name__ == "__main__":
    main()
