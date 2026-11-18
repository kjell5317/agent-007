import { stringify } from "yaml";

export function toYaml(value: unknown): string {
  return stringify(value, { sortMapEntries: true }).trimEnd();
}
