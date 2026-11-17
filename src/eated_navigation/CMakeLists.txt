cmake_minimum_required(VERSION 3.8)
project(eated_navigation)

find_package(ament_cmake REQUIRED)
install(DIRECTORY launch DESTINATION share/${PROJECT_NAME})
install(FILES README.md apt_packages.txt DESTINATION share/${PROJECT_NAME})
install(PROGRAMS scripts/check_environment.py DESTINATION lib/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_pytest_test(navigation_environment test/test_environment.py)
endif()

ament_package()
