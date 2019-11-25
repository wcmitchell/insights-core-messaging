from . import Publisher


class StdOut(Publisher):
    def publish(self, input_msg, results):
        print(f"Analysis of {input_msg}:")
        print(results)
