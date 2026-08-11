package example;

public class Service {
    private final Api api = new Api();

    public int calculate(int value) {
        return api.transform(value) + 1;
    }
}
