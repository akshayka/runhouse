import pickle

import pandas as pd

import pytest

import runhouse as rh


# https://docs.pytest.org/en/6.2.x/fixture.html#conftest-py-sharing-fixtures-across-multiple-files


@pytest.fixture
def blob_data():
    return pickle.dumps(list(range(50)))


# ----------------- Folders -----------------


@pytest.fixture
def local_folder(tmp_path):
    local_folder = rh.folder(path=tmp_path / "tests_tmp")
    local_folder.put({f"sample_file_{i}.txt": f"file{i}".encode() for i in range(3)})
    return local_folder


@pytest.fixture
def cluster_folder(cpu_cluster, local_folder):
    return local_folder.to(system=cpu_cluster)


@pytest.fixture
def s3_folder(local_folder):
    s3_folder = local_folder.to(system="s3")
    yield s3_folder

    # Delete files from S3
    s3_folder.delete_in_system()


@pytest.fixture
def gcs_folder(local_folder):
    gcs_folder = local_folder.to(system="gs")
    yield gcs_folder

    # Delete files from S3
    gcs_folder.delete_in_system()


# ----------------- Tables -----------------
@pytest.fixture
def huggingface_table():
    from datasets import load_dataset

    dataset = load_dataset("yelp_review_full", split="train[:1%]")
    return dataset


@pytest.fixture
def arrow_table():
    import pyarrow as pa

    df = pd.DataFrame(
        {
            "int": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "str": ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"],
        }
    )
    arrow_table = pa.Table.from_pandas(df)
    return arrow_table


@pytest.fixture
def cudf_table():
    import cudf

    gdf = cudf.DataFrame(
        {"id": [1, 2, 3, 4, 5, 6], "grade": ["a", "b", "b", "a", "a", "e"]}
    )
    return gdf


@pytest.fixture
def pandas_table():
    df = pd.DataFrame(
        {"id": [1, 2, 3, 4, 5, 6], "grade": ["a", "b", "b", "a", "a", "e"]}
    )
    return df


@pytest.fixture
def dask_table():
    import dask.dataframe as dd

    index = pd.date_range("2021-09-01", periods=2400, freq="1H")
    df = pd.DataFrame({"a": range(2400), "b": list("abcaddbe" * 300)}, index=index)
    ddf = dd.from_pandas(df, npartitions=10)
    return ddf


@pytest.fixture
def ray_table():
    import ray

    ds = ray.data.range(10000)
    return ds


# ----------------- Clusters -----------------


@pytest.fixture
def cluster(request):
    """Parametrize over multiple fixtures - useful for running the same test on multiple hardware types."""
    # Example: @pytest.mark.parametrize("cluster", ["v100_gpu_cluster", "k80_gpu_cluster"], indirect=True)"""
    return request.getfixturevalue(request.param)


@pytest.fixture(scope="session")
def cpu_cluster():
    c = rh.autocluster("^rh-cpu")
    c.name = "donny-rh-cpu"
    c.up_if_not()
    c.install_packages(["pytest"])
    return c


@pytest.fixture(scope="session")
def byo_cpu():
    # Spin up a new basic m5.xlarge EC2 instance
    c = rh.cluster(
        instance_type="m5.xlarge",
        provider="aws",
        region="us-east-1",
        image_id="ami-0a313d6098716f372",
        name="test-byo-cluster",
    ).up_if_not()
    c = rh.cluster(name="different-cluster", ips=[c.address], ssh_creds=c.ssh_creds())
    c.install_packages(["pytest"])
    return c


@pytest.fixture(scope="session")
def v100_gpu_cluster():
    return rh.autocluster("^rh-v100", provider="aws").up_if_not()


@pytest.fixture(scope="session")
def k80_gpu_cluster():
    return rh.autocluster(
        name="rh-k80", instance_type="K80:1", provider="aws"
    ).up_if_not()


@pytest.fixture(scope="session")
def a10g_gpu_cluster():
    return rh.autocluster(
        name="rh-a10x", instance_type="g5.2xlarge", provider="aws"
    ).up_if_not()


# ----------------- Envs -----------------


@pytest.fixture
def test_env():
    return rh.env(["pytest"])


# ----------------- Packages -----------------


@pytest.fixture
def local_package(local_folder):
    return rh.package(path=local_folder.path, install_method="local")


@pytest.fixture
def s3_package(s3_folder):
    return rh.package(
        path=s3_folder.path, system=s3_folder.system, install_method="local"
    )
