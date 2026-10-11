import java.util.Map;

class CapturedInvocation<B> {
  static <B, T extends B> T cast(Class<T> type, B value) {
    return type.cast(value);
  }

  void putAll(Map<Class<? extends B>, B> values) {
    for (Map.Entry<? extends Class<? extends B>, B> entry : values.entrySet()) {
      cast(entry.getKey(), entry.getValue());
    }
  }
}
