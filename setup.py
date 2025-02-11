import sys
import os
import subprocess
from setuptools import setup

"""
This file only will call CMake process to generate scripts, build, and then install the NuPIC binaries.
ANY EXTRA code related to build process MUST be put into CMake file.
"""

repositoryDir = os.getcwd()


# Read command line options looking for extra options for CMake and Make
# For example, an user could type:
#   python setup.py install make_options='-j3'
# which will add '-j3' option to Make commandline
cmakeOptions = ''
makeOptions = ''
setupOptions = ''
mustBuildExtensions = False
for arg in sys.argv:
  if ('cmake_options' in arg) or ('make_options' in arg):
    (option, _, rhs) = arg.partition('=')
    if option[0] == '--cmake_options':
      cmakeOptions = rhs
    if option[0] == '--make_options':
      makeOptions = rhs
  elif (not 'setup.py' in arg):
    if ('build' in arg) or ('install' in arg):
      mustBuildExtensions = True
    setupOptions += arg + ' '


# Check if no option was passed, i.e. if 'setup.py' is the only option
# If True, 'develop' is passed by default
# This is useful when a developer wish build the project directly from an IDE
if len(sys.argv) == 1:
  print "No command passed. Using 'develop' as default command. Use 'python setup.py --help' for more information."
  sys.argv.append('develop')
  mustBuildExtensions = True


# Get properties of the project like version, notes, etc
properties = {}
execfile(os.path.join(repositoryDir, 'nupic', '__init__.py'), {}, properties)


def findPackages(repositoryDir):
  """
  Traverse nupic directory and create packages for each subdir containing a
  __init__.py file
  """
  packages = []
  for root, dirs, files in os.walk(repositoryDir + '/nupic'):
    if '__init__.py' in files:
      subdir = root.replace(repositoryDir + '/', '')
      packages.append(subdir.replace('/', '.'))
  return packages


def build_extensions_nupic():
  """
  CMake-specific build operations
  """

  # Prepare directories to the CMake process
  sourceDir = repositoryDir
  buildScriptsDir = repositoryDir + '/build/scripts'
  if not os.path.exists(buildScriptsDir):
    os.makedirs(buildScriptsDir)
  os.chdir(buildScriptsDir)

  # Generate build files with CMake
  return_code = subprocess.call('cmake ' + sourceDir + ' ' + cmakeOptions, shell=True)
  if (return_code != 0):
    sys.exit("Unable to generate build scripts!")

  # Build library with Make
  return_code = subprocess.call('make ' + makeOptions, shell=True)
  if (return_code != 0):
    sys.exit("Unable to build the library!")


def setup_nupic():
  """
  Package setup operations
  """
  
  # Setup library
  os.chdir(repositoryDir)
  setup(
    name = 'nupic',
    version = properties['__version__'],
    packages = findPackages(repositoryDir),
    package_data = {
      'nupic': ['README.md', 'LICENSE.txt', '*.so', '*.dll', '*.dylib'],
      'nupic.bindings': ['_*.so', '_*.dll'],
      'nupic.data': ['*.json'],
      'nupic.frameworks.opf.exp_generator': ['*.json', '*.tpl'],
      'nupic.frameworks.opf.jsonschema': ['*.json'],
      'nupic.support.resources.images': ['*.png', '*.gif', '*.ico', '*.graffle'],
      'nupic.swarming.jsonschema': ['*.json']},
    description = "Numenta Platform for Intelligent Computing",
    author='Numenta',
    author_email='help@numenta.org',
    url='https://github.com/numenta/nupic',
    classifiers=[
      'Programming Language :: Python',
      'Programming Language :: Python :: 2',
      'License :: OSI Approved :: GNU General Public License (GPL)',
      'Operating System :: OS Independent',
      'Development Status :: 5 - Production/Stable',
      'Environment :: Console',
      'Intended Audience :: Science/Research',
      'Topic :: Scientific/Engineering :: Artificial Intelligence'
    ],
    long_description = """\
NuPIC is a library that provides the building blocks for online prediction systems. The library contains the Cortical Learning Algorithm (CLA), but also the Online Prediction Framework (OPF) that allows clients to build prediction systems out of encoders, models, and metrics.

For more information, see numenta.org or the NuPIC wiki (https://github.com/numenta/nupic/wiki).
"""
  )


# Build and setup NuPIC
if mustBuildExtensions:
  build_extensions_nupic()
setup_nupic()
