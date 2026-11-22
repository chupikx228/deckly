interface FormData {
  append(name: string, value: { uri: string; name: string; type: string }): void;
}
