#include <algorithm>
#include <execution>
#include <iostream>
#include <numeric>
#include <vector>

int main() {
    constexpr int count = 4'000'000;
    std::vector<int> values(count, 1);
    std::for_each(std::execution::par_unseq, values.begin(), values.end(),
                  [](int& value) { value += 1; });
    const long long total = std::accumulate(values.begin(), values.end(), 0LL);
    std::cout << "stdpar_total=" << total << '\n';
    return total == 2LL * count ? 0 : 1;
}
