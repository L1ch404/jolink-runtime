package example;

public class Application {
    private final Service service = new Service();
    private final Api api = new Api();

    public int answer() {
        return service.calculate(20) + api.transform(1) + Api.MULTIPLIER;
    }
}
