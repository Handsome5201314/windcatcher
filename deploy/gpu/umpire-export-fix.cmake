# Umpire 2025.03.0 + BLT may expose source-local include paths on the
# exported aggregate target when CUDA is enabled. Keep them for builds only.
get_target_property(_windcatcher_umpire_includes umpire INTERFACE_INCLUDE_DIRECTORIES)
set(_windcatcher_fixed_includes)
foreach(_dir IN LISTS _windcatcher_umpire_includes)
  if(_dir MATCHES "^${PROJECT_SOURCE_DIR}/src/umpire/[^>]+/include$")
    list(APPEND _windcatcher_fixed_includes "$<BUILD_INTERFACE:${_dir}>")
  else()
    list(APPEND _windcatcher_fixed_includes "${_dir}")
  endif()
endforeach()
set_property(TARGET umpire PROPERTY INTERFACE_INCLUDE_DIRECTORIES "${_windcatcher_fixed_includes}")
